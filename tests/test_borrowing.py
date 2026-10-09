"""Offline ownership and restart/clear seams; fixture numbers are not host bounds."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError
import pytest

from task_worker_api.borrowing_journal import BorrowingJournal, incident_digest
from task_worker_api.borrowing_observer import BorrowingObserver, inspect_gpu, nvidia_processes, process_identity, UntrackedGPUProcess, OutsideGPUFence
from task_worker_api.borrowing_protocol import (
    BorrowingBinding, BorrowingPolicy, BorrowingOwner, BorrowingOwners, BorrowingFault,
    BorrowingAck, BorrowingClear, sign_borrowing, verify_borrowing, policy_digest,
)
from task_worker_api.errors import ProtocolError

KEY=b'borrowing-fixture-reporter-key-only-32bytes'
GRANT_KEY=b'borrowing-fixture-grant-key-only-32bytes'


def policy():
    return BorrowingPolicy(driver_version='fixture-only',collection_timeout_seconds=2,
        observation_interval_seconds=1,reclaim_timeout_seconds=1,max_processes=10,max_owners=10,max_output_bytes=10000)


def binding():
    return BorrowingBinding(authority_id=uuid4(),host_id=uuid4(),boot_id=uuid4(),epoch=1,
        service_id='fixture-inference',worker_instance_id=uuid4(),gpu_uuid='GPU-'+str(uuid4()),
        policy_digest=policy_digest(policy().model_dump(mode='json')),profile_digest='a'*64)


def view(b,sequence=1,disabled=False,owners=(),now=None):
    now=now or datetime.now(timezone.utc)
    return sign_borrowing(BorrowingOwners(binding=b,report_sequence=sequence,report_captured_at=now,
        server_time=now,disabled=disabled,complete=True,owners=owners),KEY)


@pytest.fixture
def journal(tmp_path):
    tmp_path.chmod(0o700)
    j=BorrowingJournal(tmp_path/'borrow.sqlite',binding())
    j.prepare()
    j.begin_run()
    return j


def observed(j,sequence=1,primary=False,disabled=False,physical=None):
    signed=view(j.binding,sequence,disabled)
    j.record_owner_view(signed,KEY)
    j.observe(signed,KEY,physical or {},primary_owner=primary)
    return signed


def clear_for(j,owners,physical,clear_id=None):
    inspected=j.inspection(owners,KEY,physical,primary_owner=False).observation
    now=datetime.now(timezone.utc)
    return sign_borrowing(BorrowingClear(binding=j.binding,clear_id=clear_id or uuid4(),
        previous_binding_digest=inspected.previous_binding_digest,hold_generation=inspected.hold_generation,
        incidents=inspected.incidents,inspection_digest=inspected.inspection_digest,
        report_sequence=owners.observation.report_sequence,recorded_at=now,response_at=now),GRANT_KEY)


def test_idle_bootstrap_needs_physical_proof_before_first_grant_and_survives_restart(journal):
    j=journal
    assert not j.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc)) and not j.pending() and not j.has_observation()
    j.record_owner_view(view(j.binding),KEY)
    assert not j.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc)) and not j.has_observation()
    observed(j,2,primary=True)
    assert not j.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc)) and not j.has_observation() and not j.pending()
    observed(j,3)
    assert j.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc)) and j.has_observation()
    restored=BorrowingJournal(j.path,j.binding)
    assert restored.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc)) and restored.has_observation() and not restored.pending()


def test_missing_first_proof_is_ineligible_but_established_delivery_loss_is_durable(journal):
    observer=BorrowingObserver.__new__(BorrowingObserver)
    observer.journal=journal
    observer.latest_owners=None
    observer.delivery_lost()
    assert not journal.pending()
    observed(journal)
    observer.delivery_lost()
    original=journal.pending()[0]
    assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    restored=BorrowingJournal(journal.path,journal.binding)
    assert restored.pending()==[original] and restored.has_observation()


def test_incident_bytes_generation_and_original_pins_stay_fixed_across_retry(journal):
    observed(journal)
    first=journal.fault(1,'untracked_owner','1'*64)
    second=journal.fault(2,'outside_fence','2'*64)
    assert first.hold_generation==1 and second.hold_generation==2
    original=first.model_dump_json()
    for seq in (2,3):
        fault=BorrowingFault(version=1,binding=journal.binding,incident=first,
            delivery_sequence=seq,delivery_at=datetime.now(timezone.utc))
        assert verify_borrowing(sign_borrowing(fault,KEY),KEY,journal.binding).incident.model_dump_json()==original
    assert journal.pending()[0].model_dump_json()==original
    assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))


def test_ack_and_fresh_report_never_clear_fault(journal):
    original=journal.fault(1,'ownership_unverified','b'*64)
    ack=sign_borrowing(BorrowingAck(incident=original,recorded_at=datetime.now(timezone.utc),disabled=True),KEY)
    journal.acknowledge(ack,KEY)
    observed(journal,2)
    assert journal.awaiting_delivery()==[] and journal.pending()==[original]
    assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))


def test_exact_clear_confirms_lost_ack_and_preserves_original_rows(journal):
    original=journal.fault(1,'untracked_owner','b'*64)
    physical={'gpu_uuid':journal.binding.gpu_uuid,'processes':[]}
    owners=observed(journal,2,disabled=True,physical=physical)
    receipt=clear_for(journal,owners,physical)
    before=original.model_dump_json()
    journal.apply_clear(receipt,GRANT_KEY,KEY,owners,physical,primary_owner=False)
    assert not journal.pending() and not journal.awaiting_delivery()
    assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))  # Authority enabled proof is still required.
    journal.begin_run()
    observed(journal,3,physical=physical)
    assert journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    with sqlite3.connect(journal.path) as db:
        assert db.execute('SELECT payload FROM incidents').fetchone()[0]==before
        assert db.execute('SELECT count(*) FROM incident_clear').fetchone()[0]==1
    journal.apply_clear(receipt,GRANT_KEY,KEY,owners,physical,primary_owner=False)


def test_old_receipt_cannot_clear_newer_local_incident_or_partial_set(journal):
    journal.fault(1,'untracked_owner','b'*64)
    owners=observed(journal,2,disabled=True)
    old=clear_for(journal,owners,{"gpu_uuid":journal.binding.gpu_uuid,"processes":[]})
    journal.fault(2,'outside_fence','c'*64)
    with pytest.raises(ProtocolError,match='newer local'):
        journal.apply_clear(old,GRANT_KEY,KEY,owners,{"gpu_uuid":journal.binding.gpu_uuid,"processes":[]},primary_owner=False)
    current=clear_for(journal,owners,{"gpu_uuid":journal.binding.gpu_uuid,"processes":[]})
    partial=current.observation.model_copy(update={'incidents':old.observation.incidents})
    with pytest.raises(ProtocolError,match='exact retained'):
        journal.apply_clear(sign_borrowing(partial,GRANT_KEY),GRANT_KEY,KEY,owners,{"gpu_uuid":journal.binding.gpu_uuid,"processes":[]},primary_owner=False)
    assert len(journal.pending())==2
    with pytest.raises(ValidationError):
        BorrowingClear(**{**current.observation.model_dump(),'incidents':{}})


def test_binding_recovery_requires_exact_signed_clear_and_keeps_original_incident(journal):
    original=journal.fault(1,'ownership_unverified','d'*64)
    new=journal.binding.model_copy(update={'boot_id':uuid4(),'epoch':2,'worker_instance_id':uuid4()})
    restored=BorrowingJournal(journal.path,new)
    assert not restored.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    assert restored.awaiting_delivery()==[original]
    owners=view(new,1,True)
    clear=clear_for(restored,owners,{"gpu_uuid":restored.binding.gpu_uuid,"processes":[]})
    restored.apply_clear(clear,GRANT_KEY,KEY,owners,{"gpu_uuid":journal.binding.gpu_uuid,"processes":[]},primary_owner=False)
    assert not restored.pending()
    with sqlite3.connect(restored.path) as db:
        assert db.execute('SELECT payload FROM incidents').fetchone()[0]==original.model_dump_json()
    fault=BorrowingFault(version=1,binding=new,incident=original,delivery_sequence=1,
        delivery_at=datetime.now(timezone.utc))
    assert verify_borrowing(sign_borrowing(fault,KEY),KEY,new).incident.binding==journal.binding


def test_signatures_and_policy_profile_scope_cannot_be_forged(journal):
    signed=view(journal.binding)
    for changed in ({'host_id':uuid4()},{'gpu_uuid':'GPU-'+str(uuid4())},{'epoch':2},
                    {'policy_digest':'e'*64},{'profile_digest':'f'*64}):
        with pytest.raises(ProtocolError):
            verify_borrowing(signed,KEY,journal.binding.model_copy(update=changed))
    with pytest.raises(ProtocolError):
        verify_borrowing(signed,GRANT_KEY,journal.binding)
    with pytest.raises(ValidationError):
        BorrowingOwners(**{**signed.observation.model_dump(),'token':'not-allowed'})


def xml(b,kind=None,pid=42):
    process='' if kind is None else f'<process_info><pid>{pid}</pid><type>{kind}</type><process_name>ignored</process_name></process_info>'
    return f'<nvidia_smi_log><driver_version>fixture-only</driver_version><gpu><uuid>{b.gpu_uuid}</uuid><mig_mode><current_mig>N/A</current_mig></mig_mode><processes>{process}</processes></gpu></nvidia_smi_log>'.encode()


@pytest.mark.parametrize('kind',['C','G','C+G','O','M','M+C'])
def test_all_process_classes_are_recognized_and_mps_is_not_inferred_safe(kind):
    b=binding()
    if kind in ('M','M+C'):
        with pytest.raises(ProtocolError,match='MPS'):
            nvidia_processes(xml(b,kind),b,policy())
    else:
        assert nvidia_processes(xml(b,kind),b,policy())==[(42,kind)]


def proc(tmp_path,pid,cgroup,start=100):
    root=tmp_path/str(pid)
    root.mkdir(exist_ok=True)
    (root/'stat').write_text(f'{pid} (GPU child) S '+'0 '*18+f'{start} 0',encoding='utf-8')
    (root/'cgroup').write_text('0::'+cgroup,encoding='utf-8')


@pytest.mark.parametrize('state',['reserved','running','releasing','recovering'])
def test_expired_unreleased_verified_primary_owner_remains_transient(tmp_path,monkeypatch,state):
    b=binding()
    monkeypatch.setattr('task_worker_api.host_reporter.physical_boot_id',lambda *a:b.boot_id)
    proc(tmp_path,42,'/qualified/container/child')
    owner=BorrowingOwner(attempt_id=uuid4(),worker_instance_id=uuid4(),generation=1,
        execution_scope='qualified',gpu_uuid=b.gpu_uuid,state=state,
        lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1),reclaim_deadline=None,role='primary',service_id=None,engine_epoch=None)
    proof={'attempt_id':str(owner.attempt_id),'container_id':'container-id','pid':40,
        'start_time':50,'cgroup':'/qualified/container','role':'primary'}
    supervisor=SimpleNamespace(execution_scope='qualified',verified_gpu_launch=lambda *a,**kw:proof)
    physical,primary=inspect_gpu(view(b,owners=(owner,)).observation,policy(),
        {owner.worker_instance_id:supervisor},proc_root=tmp_path,raw=xml(b,'G'))
    assert primary and physical['processes'][0]['pid']==42
    with pytest.raises(UntrackedGPUProcess):
        inspect_gpu(view(b).observation,policy(),{},proc_root=tmp_path,raw=xml(b,'G'))


def test_pid_start_and_cgroup_identity_is_current(tmp_path):
    proc(tmp_path,42,'/qualified/container',start=100)
    assert process_identity(42,tmp_path)==(100,'/qualified/container')
    proc(tmp_path,42,'/qualified/foreign',start=200)
    assert process_identity(42,tmp_path)==(200,'/qualified/foreign')
    (tmp_path/'42/cgroup').write_text('0::/qualified/../foreign',encoding='utf-8')
    with pytest.raises(ProtocolError):
        process_identity(42,tmp_path)


def test_actual_docker_accessor_shares_launch_checks_and_fences_profile_epoch(tmp_path,monkeypatch):
    from tests.test_services import grant,profile
    from task_worker_api.docker_supervisor import DockerSupervisor
    from task_worker_api.resources import AdmissionError
    gpu='GPU-'+str(uuid4())
    value=grant(profile=profile(gpu_count=1,gpu_backend='cuda',gpu_vram_mib=10),gpu_uuid=gpu)
    monkeypatch.setattr('task_worker_api.docker_supervisor.physical_boot_id',lambda *a:value.boot_id)
    instance=DockerSupervisor(tmp_path/'private.sqlite',tmp_path/'work',authority_id=value.authority_id,
        host_id=value.host_id,boot_id=value.boot_id,epoch=value.epoch,execution_scope=value.execution_scope,
        cgroup_parent='',network_id='d'*64)
    proc(tmp_path,100,'/qualified/container',start=200)
    running=[False]
    calls=[]
    container='b'*64
    def docker(*args,**kwargs):
        calls.append(args)
        if args[0]=='create':return container
        if args[0]=='start':running[0]=True
        if args[0]=='kill':running[0]=False
        if args[0]=='inspect':
            plan=instance._row(value)[0]
            return json.dumps([dict(Id=container,Image=plan['image'],
                Config={'User':plan['user'],'Labels':{'synpusher.launch':plan['id']}},
                State={'Running':running[0],'Pid':100 if running[0] else 0},
                NetworkSettings={'Networks':{'fixture':{'NetworkID':plan['network_id']}}},
                HostConfig=dict(NetworkMode=plan['network_id'],Privileged=False,PidMode='',CapAdd=None,
                    Devices=[],RestartPolicy={'Name':'no'},CgroupParent='',
                    DeviceRequests=[{'DeviceIDs':[gpu],'Count':0}],Memory=100*1024*1024,NanoCpus=1000000000,
                    ReadonlyRootfs=True,CapDrop=['ALL'],SecurityOpt=['no-new-privileges']))])
        return ''
    monkeypatch.setattr(instance,'_docker',docker)
    instance.launch_service(value,SimpleNamespace(grant=value,require_live=lambda:None),
        image='sha256:'+'a'*64,command=['fixture-engine'],read_only_mounts={})
    owner=BorrowingOwner(attempt_id=value.ownership.attempt_id,worker_instance_id=value.ownership.worker_instance_id,
        generation=value.ownership.generation,execution_scope=value.execution_scope,gpu_uuid=gpu,
        state='running',lease_expires_at=value.lease_expires_at,reclaim_deadline=None,role='inference',
        service_id=value.profile.service_id,engine_epoch=value.engine_epoch)
    before=len(calls)
    proof=instance.verified_gpu_launch(owner,proc_root=tmp_path,profile_digest=value.profile.config_digest)
    assert proof['pid']==100 and proof['start_time']==200 and proof['cgroup']=='/qualified/container'
    assert all(call[0]=='inspect' for call in calls[before:])
    assert 'token' not in proof and 'task' not in proof
    with pytest.raises(AdmissionError,match='fenced'):
        instance.verified_gpu_launch(owner.model_copy(update={'engine_epoch':uuid4()}),proc_root=tmp_path,profile_digest=value.profile.config_digest)
    with pytest.raises(AdmissionError,match='fenced'):
        instance.verified_gpu_launch(owner,proc_root=tmp_path,profile_digest='f'*64)
    assert instance.stop_verified_gpu_inference(owner,profile_digest=value.profile.config_digest,timeout=1,proc_root=tmp_path)
    assert calls[-1]==('kill','--signal=KILL',container)
    assert instance._row(value)[1]=='started'  # Observer withdrawal never signs cleanup or releases accounting.
    with pytest.raises(AdmissionError,match='service_grant_required'):
        instance.stop_verified_gpu_inference(owner.model_copy(update={'role':'primary'}),profile_digest=value.profile.config_digest,timeout=1,proc_root=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('still_present',[False,True])
async def test_release_between_scan_and_refresh_rescans_physical_evidence(journal,monkeypatch,still_present):
    observer=BorrowingObserver.__new__(BorrowingObserver)
    observer.binding=journal.binding
    observer.key=KEY
    observer.policy=policy()
    observer.journal=journal
    observer.supervisors={}
    observer.latest_owners=observed(journal)
    scans=[]
    def scan(*args,**kwargs):
        scans.append(args[0].report_sequence)
        if len(scans)==1 or still_present:
            raise UntrackedGPUProcess('GPU process lacks exact owned launch proof')
        return {'processes':[]},False
    monkeypatch.setattr('task_worker_api.borrowing_observer.inspect_gpu',scan)
    async def refreshed():
        signed=view(journal.binding,2)
        observer.accept_owners(signed.model_dump(mode='json'))
        return signed
    await observer.observe_once(refreshed)
    assert scans==[1,2]
    assert bool(journal.pending())==still_present
    assert journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))!=still_present


@pytest.mark.asyncio
async def test_fault_retries_do_not_allocate_or_refresh_hardware_sequence(journal,monkeypatch):
    import httpx
    observer=BorrowingObserver.__new__(BorrowingObserver)
    observer.binding=journal.binding
    observer.key=KEY
    observer.policy=policy()
    observer.journal=journal
    observer.supervisors={}
    observer.latest_owners=observed(journal,9)
    journal.finish_run()
    original=journal.fault(9,'untracked_owner','9'*64)
    monkeypatch.setattr('task_worker_api.borrowing_observer.inspect_gpu',lambda *a,**kw:({},False))
    sent=[]
    async def refreshed():
        pytest.fail('Fault retry must not publish or allocate hardware reports')
    class Client:
        async def post(self,url,**kwargs):
            sent.append(kwargs['json'])
            return httpx.Response(503,request=httpx.Request('POST',url))
    async def tick(seconds):
        if len(sent)>=3:raise asyncio.CancelledError
    monkeypatch.setattr('task_worker_api.borrowing_observer.asyncio.sleep',tick)
    with pytest.raises(asyncio.CancelledError):
        await observer.run(Client(),'http://fixture/api/v1',refreshed)
    assert len(sent)==3
    assert all(item['observation']['delivery_sequence']==9 for item in sent)
    assert all(item['observation']['incident']==original.model_dump(mode='json') for item in sent)
    assert journal.owner_view().observation.report_sequence==9
    assert journal.pending()==[original]


def test_actual_wal_drift_is_rejected_without_switching_mode_or_losing_history(journal):
    observed(journal)
    incident=journal.fault(1,'untracked_owner','1'*64)
    with sqlite3.connect(journal.path) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
    assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    with pytest.raises(ProtocolError,match='mode changed'):
        journal.pending()
    with sqlite3.connect(journal.path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
        assert db.execute('SELECT payload FROM incidents').fetchone()[0]==incident.model_dump_json()


@pytest.mark.parametrize('failure',['insert','begin','reacquire'])
def test_real_second_connection_cannot_use_old_proof_after_journal_write_failure(journal,monkeypatch,failure):
    observed(journal)
    connection=journal._connect
    blocker=None
    if failure in ('begin','reacquire'):
        blocker=sqlite3.connect(journal.path,timeout=0)
        blocker.execute('BEGIN IMMEDIATE')
    class Injected:
        def __init__(self,db):self.db=db
        def __getattr__(self,name):return getattr(self.db,name)
        def execute(self,sql,*args):
            if sql.startswith('INSERT INTO incidents'):
                raise sqlite3.OperationalError('injected disk full')
            return self.db.execute(sql,*args)
    if failure=='insert':
        monkeypatch.setattr(journal,'_connect',lambda **kw:Injected(connection(**kw)))
    with pytest.raises(sqlite3.OperationalError):
        journal.fault(1,'untracked_owner','f'*64)
    worker=BorrowingJournal(journal.path,journal.binding)
    assert not worker.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    if blocker is not None:
        blocker.rollback()
        blocker.close()
    if journal._failed_connection is not None:
        journal._failed_connection.close()  # Simulated failed observer death releases its actual lock.
        journal._failed_connection=None
    # After that death, a new worker's post-start barrier cannot use old readable proof.
    restarted=BorrowingJournal(journal.path,journal.binding)
    assert not restarted.eligible(KEY,after=datetime.now(timezone.utc))
    with sqlite3.connect(journal.path) as db:
        assert db.execute('SELECT count(*) FROM incidents').fetchone()[0]==0
        assert db.execute('SELECT running FROM state').fetchone()[0]==1
    # Observer restart preserves the unclean established state as an explicit unresolved fault.
    restarted.begin_run()
    assert len(restarted.pending())==1
    assert restarted.pending()[0].reason=='ownership_unverified'
    observed(restarted,2)
    assert not restarted.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))


def test_clean_idle_observer_restart_needs_new_proof_without_fabricating_fault(journal):
    observed(journal)
    journal.finish_run()
    restored=BorrowingJournal(journal.path,journal.binding)
    restored.begin_run()
    assert not restored.pending()
    assert not restored.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    observed(restored,2)
    assert restored.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))


def test_actual_observer_process_death_releases_failure_lock_but_never_old_proof(journal):
    import os
    import select
    import signal
    import subprocess
    import sys

    script = r"""
import json,signal,sqlite3,sys
from datetime import datetime,timezone
from pathlib import Path
from task_worker_api.borrowing_journal import BorrowingJournal
from task_worker_api.borrowing_protocol import BorrowingBinding,SignedBorrowingOwners
fixture=json.loads(sys.stdin.read())
journal=BorrowingJournal(Path(fixture['path']),BorrowingBinding.model_validate(fixture['binding']))
journal.begin_run()
view=SignedBorrowingOwners.model_validate(fixture['view'])
key=bytes.fromhex(fixture['key'])
journal.record_owner_view(view,key)
journal.observe(view,key,{},primary_owner=False)
connect=journal._connect
class Failing:
 def __init__(self,db):self.db=db
 def __getattr__(self,name):return getattr(self.db,name)
 def execute(self,sql,*args):
  if sql.startswith('INSERT INTO incidents'):raise sqlite3.OperationalError('synthetic disk full')
  return self.db.execute(sql,*args)
journal._connect=lambda **kw:Failing(connect(**kw))
try:journal.fault(1,'untracked_owner','f'*64)
except sqlite3.OperationalError:pass
assert journal._failed_connection is not None
print('held',flush=True)
signal.pause()
"""
    child=subprocess.Popen([sys.executable,'-B','-c',script],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,text=True,env=dict(os.environ))
    try:
        child.stdin.write(json.dumps({'path':str(journal.path),'binding':journal.binding.model_dump(mode='json'),
            'view':view(journal.binding).model_dump(mode='json'),'key':KEY.hex()}))
        child.stdin.close()
        assert select.select([child.stdout],[],[],5)[0], 'failure-lock child did not initialize'
        assert child.stdout.readline().strip()=='held'
        assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=5)==-signal.SIGTERM
        # The OS released the actual failed writer lock; a dead observer's proof stays held.
        with sqlite3.connect(journal.path,timeout=0) as db:
            db.execute('BEGIN IMMEDIATE')
            assert db.execute('SELECT count(*) FROM incidents').fetchone()[0]==0
            assert db.execute('SELECT running FROM state').fetchone()[0]==1
        assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
        restarted=BorrowingJournal(journal.path,journal.binding)
        assert not restarted.eligible(KEY,after=datetime.now(timezone.utc))
        restarted.begin_run()
        assert len(restarted.pending())==1
        observed(restarted,2)
        assert not restarted.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    finally:
        if child.poll() is None:
            child.kill();child.wait(timeout=5)
        child.stdout.close();child.stderr.close()


def test_failed_hold_persistence_stops_only_exact_borrowed_owner(journal,monkeypatch):
    b=journal.binding
    inference=BorrowingOwner(attempt_id=uuid4(),worker_instance_id=b.worker_instance_id,generation=1,
        execution_scope='qualified',gpu_uuid=b.gpu_uuid,state='running',lease_expires_at=datetime.now(timezone.utc),
        reclaim_deadline=None,role='inference',service_id=b.service_id,engine_epoch=uuid4())
    primary=inference.model_copy(update={'attempt_id':uuid4(),'worker_instance_id':uuid4(),'role':'primary',
        'service_id':None,'engine_epoch':None})
    observer=BorrowingObserver.__new__(BorrowingObserver)
    observer.binding=b;observer.key=KEY;observer.policy=policy();observer.journal=journal
    observer.latest_owners=view(b,owners=(inference,primary))
    stopped=[]
    observer.supervisors={inference.worker_instance_id:SimpleNamespace(stop_verified_gpu_inference=
        lambda owner,**kw:stopped.append((owner,kw))),primary.worker_instance_id:SimpleNamespace(stop_verified_gpu_inference=
        lambda *a,**kw:pytest.fail('Primary owner must never be stopped by borrowing observer'))}
    monkeypatch.setattr(journal,'fault',lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('synthetic disk full')))
    with pytest.raises(sqlite3.OperationalError):observer.hold(1,'ownership_unverified','1'*64)
    assert [row[0] for row in stopped]==[inference]
    assert stopped[0][1]=={'profile_digest':b.profile_digest,'timeout':policy().reclaim_timeout_seconds}
    assert not journal.pending()  # Failed persistence is never reported as a durable incident.


@pytest.mark.parametrize('cause',['primary_revocation','residence_expiry','explicit_release'])
def test_existing_resident_reclaim_bound_defers_without_permanent_incident(journal,tmp_path,monkeypatch,cause):
    b=journal.binding
    monkeypatch.setattr('task_worker_api.host_reporter.physical_boot_id',lambda *a:b.boot_id)
    proc(tmp_path,42,'/qualified/container/engine')
    now=datetime.now(timezone.utc)
    owner=BorrowingOwner(attempt_id=uuid4(),worker_instance_id=b.worker_instance_id,generation=1,
        execution_scope='qualified',gpu_uuid=b.gpu_uuid,state='recovering',
        lease_expires_at=now+timedelta(seconds=30) if cause=='primary_revocation' else now-timedelta(seconds=1),
        reclaim_deadline=now+timedelta(seconds=5),role='inference',service_id=b.service_id,engine_epoch=uuid4())
    proof={'attempt_id':str(owner.attempt_id),'container_id':'owned-engine','pid':40,'start_time':50,
        'cgroup':'/qualified/container','role':'inference'}
    supervisor=SimpleNamespace(execution_scope='qualified',verified_gpu_launch=lambda *a,**kw:proof)
    signed=view(b,owners=(owner,))
    physical,primary=inspect_gpu(signed.observation,policy(),{owner.worker_instance_id:supervisor},
        proc_root=tmp_path,raw=xml(b,'C'))
    journal.record_owner_view(signed,KEY);journal.observe(signed,KEY,physical,primary_owner=primary)
    assert not primary and not journal.pending()
    assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    # The same immutable existing deadline, not a grace from a newer report, bounds cleanup.
    overdue=owner.model_copy(update={'reclaim_deadline':now-timedelta(seconds=1)})
    with pytest.raises(OutsideGPUFence):
        inspect_gpu(view(b,2,owners=(overdue,)).observation,policy(),{owner.worker_instance_id:supervisor},
            proc_root=tmp_path,raw=xml(b,'C'))
    # No physical process means no contradicted cleanup fence even if authority still holds the owner.
    empty,primary=inspect_gpu(view(b,2,owners=(overdue,)).observation,policy(),
        {owner.worker_instance_id:supervisor},proc_root=tmp_path,raw=xml(b))
    assert empty['processes']==[] and not primary


def test_owner_deadline_is_required_and_cannot_replace_actual_lease_timestamp():
    b=binding();now=datetime.now(timezone.utc)
    row=dict(attempt_id=uuid4(),worker_instance_id=b.worker_instance_id,generation=1,execution_scope='qualified',
        gpu_uuid=b.gpu_uuid,state='running',lease_expires_at=now,role='inference',service_id=b.service_id,engine_epoch=uuid4())
    with pytest.raises(ValidationError):BorrowingOwner(**row)
    with pytest.raises(ValidationError):BorrowingOwner(**row,reclaim_deadline=now)
    with pytest.raises(ValidationError):BorrowingOwner(**{**row,'state':'recovering'},reclaim_deadline=None)
    owner=BorrowingOwner(**{**row,'state':'recovering'},reclaim_deadline=now+timedelta(seconds=1))
    assert owner.lease_expires_at==now


def test_report_ack_selects_exact_prepared_binding_and_does_not_accept_stale_other_gpu(journal):
    observer=BorrowingObserver.__new__(BorrowingObserver)
    observer.binding=journal.binding;observer.key=KEY;observer.journal=journal;observer.latest_owners=None
    target=view(journal.binding)
    other=view(journal.binding.model_copy(update={'gpu_uuid':'GPU-'+str(uuid4())}))
    assert observer.accept_report_ack({'sequence':1,'borrowing_owners':[other.model_dump(mode='json'),
        target.model_dump(mode='json')]},1)==target
    for body in ({'sequence':1},{'sequence':1,'borrowing_owners':[other.model_dump(mode='json')]},
                 {'sequence':2,'borrowing_owners':[target.model_dump(mode='json')]},
                 {'sequence':1,'borrowing_owners':[target.model_dump(mode='json')]*2}):
        with pytest.raises(ProtocolError):observer.accept_report_ack(body,1)


@pytest.mark.asyncio
async def test_fault_delivery_runs_while_primary_hardware_sequences_continue_unchanged(journal,tmp_path,monkeypatch):
    import httpx
    from task_worker_api import host_reporter
    from task_worker_api.resources import HostSnapshot
    b=journal.binding
    (tmp_path/'reporter.key').write_bytes(KEY)
    config=dict(authority_id=str(b.authority_id),host_id=str(b.host_id),boot_id=str(b.boot_id),epoch=b.epoch,
        signing_key_file=str(tmp_path/'reporter.key'),state_file=str(tmp_path/'primary.sqlite'),
        report_file=str(tmp_path/'primary.json'),backend_url='http://fixture/api/v1',borrowing={})
    observer=BorrowingObserver.__new__(BorrowingObserver)
    observer.binding=b;observer.policy=policy();observer.key=KEY;observer.journal=journal
    observer.latest_owners=None;observer.supervisors={}
    original=journal.fault(1,'untracked_owner','e'*64)
    monkeypatch.setattr('task_worker_api.borrowing_observer.BorrowingObserver',lambda config:observer)
    monkeypatch.setattr('task_worker_api.borrowing_observer.inspect_gpu',lambda *a,**kw:({},False))
    def snapshot(config,sequence):
        return HostSnapshot(host_id=b.host_id,boot_id=b.boot_id,sequence=sequence,captured_at=datetime.now(timezone.utc),
            host_ram={'allocatable':100,'available':100},cpu_millicores=100,execution_scopes={},scratch_pools={},gpus={})
    monkeypatch.setattr(host_reporter,'_snapshot',snapshot)
    sent_reports=[];faults=[];real_client=httpx.AsyncClient;real_sleep=asyncio.sleep
    def reply(request):
        body=json.loads(request.content)
        if request.url.path.endswith('/host-report'):
            sequence=body['report']['sequence'];sent_reports.append(sequence)
            signed=view(b,sequence,True)
            assert body['signature']==host_reporter.observation_signature('hardware',b.authority_id,b.epoch,body['report'],KEY)
            return httpx.Response(200,json={'sequence':sequence,'borrowing_owners':[signed.model_dump(mode='json')]})
        assert request.url.path.endswith('/borrowing-fault')
        faults.append(body)
        if len(faults)<3:return httpx.Response(503)
        ack=sign_borrowing(BorrowingAck(incident=original,recorded_at=datetime.now(timezone.utc),disabled=True),KEY)
        return httpx.Response(200,json=ack.model_dump(mode='json'))
    async def tick(seconds):
        if seconds==5 and len(sent_reports)>=8:raise asyncio.CancelledError
        await real_sleep(.002 if seconds==5 else .001)
    monkeypatch.setattr(host_reporter.asyncio,'sleep',tick)
    monkeypatch.setattr(host_reporter.httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(reply),**kw))
    with pytest.raises(asyncio.CancelledError):await host_reporter.run(config)
    assert sent_reports==list(range(1,9))
    assert len(faults)>=3 and journal.pending()==[original] and journal.awaiting_delivery()==[]
    assert all(row['observation']['incident']==original.model_dump(mode='json') for row in faults)
    assert all(row['observation']['delivery_sequence'] in sent_reports for row in faults)
    with sqlite3.connect(config['state_file']) as db:
        assert db.execute('SELECT sequence FROM reporter').fetchone()[0]==8


def test_historical_failed_task_old_barrier_requires_a_fresh_action_barrier(journal):
    claimed_at=datetime.now(timezone.utc)
    observed(journal)
    worker=BorrowingJournal(journal.path,journal.binding)
    assert worker.eligible(KEY,after=claimed_at)
    with sqlite3.connect(journal.path,timeout=0) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):journal.fault(1,'untracked_owner','f'*64)
        assert journal._write_failed and journal._failed_connection is None
        assert not worker.eligible(KEY,after=claimed_at)
        blocker.rollback()
    # Historical unsafe caller: an already-satisfied barrier cannot detect task death in a shared PID.
    assert worker.eligible(KEY,after=claimed_at)
    assert not worker.eligible(KEY,after=datetime.now(timezone.utc))


def test_readonly_observer_preparation_never_creates_missing_launch_journal(tmp_path,monkeypatch):
    from task_worker_api.docker_supervisor import DockerSupervisor
    b=binding();tmp_path.chmod(0o700)
    key=tmp_path/'key';key.write_bytes(KEY);key.chmod(0o600)
    root=tmp_path/'work';root.mkdir()
    missing=tmp_path/'missing.sqlite'
    config={'binding':b.model_dump(mode='json'),'policy':policy().model_dump(mode='json'),
        'journal':str(tmp_path/'borrow.sqlite'),'reporter_key_file':str(key),'launchers':[dict(
        worker_instance_id=str(b.worker_instance_id),journal=str(missing),work_root=str(root),execution_scope='qualified',
        cgroup_parent='qualified',network_id='d'*64)]}
    with pytest.raises(FileNotFoundError):BorrowingObserver(config)
    assert not missing.exists() and not (tmp_path/'borrow.sqlite').exists()
    kwargs=dict(authority_id=b.authority_id,host_id=b.host_id,boot_id=b.boot_id,epoch=b.epoch,
        execution_scope='qualified',cgroup_parent='qualified',network_id='d'*64)
    with pytest.raises(sqlite3.OperationalError):DockerSupervisor(missing,root,observe_only=True,**kwargs)
    assert not missing.exists()
    missing.write_bytes(b'');missing.chmod(0o600)
    with pytest.raises(sqlite3.OperationalError):BorrowingObserver(config)
    assert missing.read_bytes()==b''  # Missing schema cannot be silently prepared by observation.


@pytest.mark.asyncio
async def test_gpu_pid_exit_between_nvidia_sample_and_proc_read_refreshes_before_fault(journal,monkeypatch):
    observer=BorrowingObserver.__new__(BorrowingObserver)
    observer.binding=journal.binding;observer.key=KEY;observer.policy=policy();observer.journal=journal
    observer.supervisors={};observer.latest_owners=observed(journal)
    scans=[]
    def scan(*args,**kwargs):
        scans.append(args[0].report_sequence)
        if len(scans)==1:raise FileNotFoundError('synthetic GPU PID exited during owned cleanup')
        return {'processes':[]},False
    monkeypatch.setattr('task_worker_api.borrowing_observer.inspect_gpu',scan)
    async def refreshed():
        signed=view(journal.binding,2);observer.accept_owners(signed.model_dump(mode='json'));return signed
    await observer.observe_once(refreshed)
    assert scans==[1,2] and not journal.pending()


@pytest.mark.parametrize('replay',[False,True])
def test_local_clear_requires_no_unreleased_owner_even_before_engine_launch(journal,replay):
    journal.fault(1,'untracked_owner','b'*64)
    physical={'gpu_uuid':journal.binding.gpu_uuid,'processes':[]}
    idle=observed(journal,2,disabled=True,physical=physical)
    receipt=clear_for(journal,idle,physical)
    if replay:journal.apply_clear(receipt,GRANT_KEY,KEY,idle,physical,primary_owner=False)
    b=journal.binding
    owner=BorrowingOwner(attempt_id=uuid4(),worker_instance_id=b.worker_instance_id,generation=1,execution_scope='qualified',
        gpu_uuid=b.gpu_uuid,state='reserved',lease_expires_at=datetime.now(timezone.utc)+timedelta(seconds=30),
        reclaim_deadline=None,role='inference',service_id=b.service_id,engine_epoch=uuid4())
    current=view(b,3,owners=(owner,))
    # Authority may grant after its clear; local receipt application still requires a genuinely idle state.
    with pytest.raises(ProtocolError):
        journal.apply_clear(receipt,GRANT_KEY,KEY,current,physical,primary_owner=False)


@pytest.mark.parametrize('physical',[{}, {'gpu_uuid':'GPU-'+str(uuid4()),'processes':[]},
    {'gpu_uuid':None,'processes':[{'pid':42}]}])
def test_local_inspection_and_clear_reject_nonidle_or_foreign_physical_view(journal,physical):
    journal.fault(1,'untracked_owner','b'*64)
    actual={'gpu_uuid':journal.binding.gpu_uuid,'processes':[]}
    owners=observed(journal,2,disabled=True,physical=actual)
    receipt=clear_for(journal,owners,actual)
    with pytest.raises(ProtocolError):journal.inspection(owners,KEY,physical,primary_owner=False)
    with pytest.raises(ProtocolError):journal.apply_clear(receipt,GRANT_KEY,KEY,owners,physical,primary_owner=False)
    assert len(journal.pending())==1


@pytest.mark.asyncio
async def test_established_observer_task_exit_records_hold_while_primary_reports_continue(journal,tmp_path,monkeypatch):
    import httpx
    from task_worker_api import host_reporter
    from task_worker_api.resources import HostSnapshot
    b=journal.binding;observed(journal)
    (tmp_path/'reporter.key').write_bytes(KEY)
    config=dict(authority_id=str(b.authority_id),host_id=str(b.host_id),boot_id=str(b.boot_id),epoch=b.epoch,
        signing_key_file=str(tmp_path/'reporter.key'),state_file=str(tmp_path/'primary.sqlite'),
        report_file=str(tmp_path/'primary.json'),backend_url='http://fixture/api/v1',borrowing={})
    observer=BorrowingObserver.__new__(BorrowingObserver)
    observer.binding=b;observer.policy=policy();observer.key=KEY;observer.journal=journal
    observer.latest_owners=journal.owner_view();observer.supervisors={}
    async def failed(*args):raise RuntimeError('synthetic observer task stopped')
    observer.run=failed
    monkeypatch.setattr('task_worker_api.borrowing_observer.BorrowingObserver',lambda config:observer)
    def snapshot(config,sequence):
        return HostSnapshot(host_id=b.host_id,boot_id=b.boot_id,sequence=sequence,captured_at=datetime.now(timezone.utc),
            host_ram={'allocatable':100,'available':100},cpu_millicores=100,execution_scopes={},scratch_pools={},gpus={})
    monkeypatch.setattr(host_reporter,'_snapshot',snapshot)
    sent=[];real_client=httpx.AsyncClient;real_sleep=asyncio.sleep
    def reply(request):
        body=json.loads(request.content);sequence=body['report']['sequence'];sent.append(sequence)
        return httpx.Response(200,json={'sequence':sequence,'borrowing_owners':[view(b,sequence).model_dump(mode='json')]})
    async def tick(seconds):
        if len(sent)>=4:raise asyncio.CancelledError
        await real_sleep(.002)
    monkeypatch.setattr(host_reporter.asyncio,'sleep',tick)
    monkeypatch.setattr(host_reporter.httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(reply),**kw))
    with pytest.raises(asyncio.CancelledError):await host_reporter.run(config)
    assert sent==[1,2,3,4]
    assert len(journal.pending())==1
    assert not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))


def test_lost_clear_history_reconciles_only_older_subset_and_preserves_new_hold(journal):
    first=journal.fault(1,'untracked_owner','b'*64)
    physical={'gpu_uuid':journal.binding.gpu_uuid,'processes':[]}
    owners=observed(journal,2,disabled=True,physical=physical)
    old=clear_for(journal,owners,physical)
    second=journal.fault(2,'outside_fence','c'*64)
    with sqlite3.connect(journal.path) as db:
        binding_before=db.execute('SELECT * FROM binding').fetchall()
        state_before=db.execute('SELECT * FROM state').fetchall()
        incidents_before=db.execute('SELECT * FROM incidents').fetchall()
    with pytest.raises(ProtocolError):journal.apply_clear(old,GRANT_KEY,KEY,owners,physical,primary_owner=False)
    journal.reconcile_clear_history(old,GRANT_KEY)
    assert journal.pending()==[second] and not journal.eligible(KEY,after=datetime.min.replace(tzinfo=timezone.utc))
    with sqlite3.connect(journal.path) as db:
        assert db.execute('SELECT * FROM binding').fetchall()==binding_before
        assert db.execute('SELECT * FROM state').fetchall()==state_before
        assert db.execute('SELECT * FROM incidents').fetchall()==incidents_before
        assert db.execute('SELECT history_only FROM clears').fetchone()==(1,)
    journal.reconcile_clear_history(old,GRANT_KEY)
    fresh=clear_for(journal,owners,physical)
    journal.apply_clear(fresh,GRANT_KEY,KEY,owners,physical,primary_owner=False)
    assert not journal.pending()
    # Replaying history after the later full clear never changes eligibility/proofs.
    journal.reconcile_clear_history(old,GRANT_KEY)
    with sqlite3.connect(journal.path) as db:
        assert db.execute('SELECT count(*) FROM clears').fetchone()[0]==2


def test_history_reconciliation_rejects_fullset_unknown_foreign_and_altered_receipts(journal):
    journal.fault(1,'untracked_owner','b'*64)
    physical={'gpu_uuid':journal.binding.gpu_uuid,'processes':[]}
    owners=observed(journal,2,disabled=True,physical=physical)
    receipt=clear_for(journal,owners,physical)
    with pytest.raises(ProtocolError):journal.reconcile_clear_history(receipt,GRANT_KEY)
    journal.fault(2,'outside_fence','c'*64)
    original=receipt.observation
    for changed in ({'binding':original.binding.model_copy(update={'gpu_uuid':'GPU-'+str(uuid4())})},
                    {'incidents':{uuid4():'1'*64}}, {'incidents':{next(iter(original.incidents)):'1'*64}},
                    {'hold_generation':2}, {'incidents':{incident.incident_id:incident_digest(incident) for incident in journal.pending()}}):
        altered=sign_borrowing(original.model_copy(update=changed),GRANT_KEY)
        with pytest.raises(ProtocolError):journal.reconcile_clear_history(altered,GRANT_KEY)
    assert len(journal.pending())==2
    current=clear_for(journal,owners,physical)
    journal.apply_clear(current,GRANT_KEY,KEY,owners,physical,primary_owner=False)
    with pytest.raises(ProtocolError):journal.reconcile_clear_history(current,GRANT_KEY)
