import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import ValidationError
import pytest

from task_worker_api.client import BackendClient
from task_worker_api.docker_supervisor import DockerSupervisor
from task_worker_api.errors import ProtocolError
from task_worker_api.resource_protocol import SignedHostReport, observation_signature
from task_worker_api.resources import AdmissionError, AttemptOwnership, CleanupEvidence, HostSnapshot
from task_worker_api.service_execution import ResidentServiceLease
from task_worker_api.service_protocol import (ServiceClaimBody, ServiceGrant, ServiceProfile, ServiceState,
    SignedServiceGrant, sign_service_grant, validate_service_state, verify_service_grant)

KEY=b'synthetic-service-grant-key-only!!'


def profile(**changes):
    return ServiceProfile(**{**dict(profile_id='inference',revision=1,service_id='synthetic-service',
        model_id='synthetic-model',model_revision='r1',config_digest='a'*64,destination='synthetic-worker',
        data_scope='public_synthetic',max_residence_seconds=60,reclaim_timeout_seconds=1,
        gpu_count=0,gpu_backend='none',gpu_vram_mib=0,host_ram_mib=100,execution_ram_mib=100,
        cpu_millicores=1000,scratch_mib=0,scratch_pool='synthetic-pool',
        evidence='synthetic tests only',validation_state='validated'),**changes})


def grant(seconds=10, **changes):
    now=datetime.now(timezone.utc)
    return ServiceGrant(**{**dict(ownership=AttemptOwnership(worker_instance_id=uuid4(),attempt_id=uuid4(),
        generation=1,token='synthetic-token-only-'+'x'*32),profile=profile(),authority_id=uuid4(),host_id=uuid4(),
        boot_id=uuid4(),epoch=1,engine_epoch=uuid4(),execution_scope='synthetic-scope',gpu_uuid=None,
        server_time=now,lease_expires_at=now+timedelta(seconds=seconds),
        residence_deadline=now+timedelta(seconds=60),state='warming'),**changes})


def state(value, phase='warming', seconds=10, **changes):
    now=datetime.now(timezone.utc)
    return ServiceState(**{**dict(attempt_id=value.ownership.attempt_id,worker_instance_id=value.ownership.worker_instance_id,
        generation=value.ownership.generation,authority_id=value.authority_id,host_id=value.host_id,boot_id=value.boot_id,
        epoch=value.epoch,engine_epoch=value.engine_epoch,server_time=now,
        lease_expires_at=min(value.residence_deadline,now+timedelta(seconds=seconds)),
        residence_deadline=value.residence_deadline,state=phase),**changes})


def report(value):
    return SignedHostReport(authority_id=value.authority_id,epoch=value.epoch,signature='0'*64,
        report=HostSnapshot(host_id=value.host_id,boot_id=value.boot_id,sequence=1,
            captured_at=datetime.now(timezone.utc),host_ram={'allocatable':100,'available':100},
            cpu_millicores=1000,execution_scopes={},scratch_pools={},gpus={}))


@pytest.mark.parametrize('changes',[{'host_ram_mib':True},{'execution_ram_mib':101},
    {'gpu_count':1},{'gpu_vram_mib':1},{'max_residence_seconds':0},{'data_scope':'patient'},
    {'task_type':'dummy'},{'case_id':1},{'reclaim_timeout_seconds':61}])
def test_service_profile_rejects_invalid_or_task_budgets(changes):
    with pytest.raises(ValidationError):profile(**changes)


def test_signed_grant_binds_profile_scope_engine_and_purpose():
    value=grant();signed=sign_service_grant(value,KEY)
    assert verify_service_grant(signed,KEY)==value
    assert 'task_id' not in value.model_dump() and 'case_id' not in value.model_dump()
    for changed in (value.model_copy(update={'engine_epoch':uuid4()}),
        value.model_copy(update={'profile':profile(model_id='different')})):
        with pytest.raises(ProtocolError):verify_service_grant(signed.model_copy(update={'grant':changed}),KEY)
    with pytest.raises(ProtocolError):verify_service_grant(signed,b'wrong-synthetic-key'*2)
    other_purpose=observation_signature('cleanup',value.authority_id,value.epoch,value.model_dump(mode='json'),KEY)
    with pytest.raises(ProtocolError):verify_service_grant(signed.model_copy(update={'signature':other_purpose}),KEY)
    with pytest.raises(ValidationError):SignedServiceGrant(**{**signed.model_dump(),'epoch':2})
    for changes in ({'lease_expires_at':value.residence_deadline+timedelta(seconds=1)},
                    {'residence_deadline':value.server_time+timedelta(seconds=61)},
                    {'lease_expires_at':value.server_time},
                    {'gpu_uuid':'GPU-not-a-uuid'}, {'profile':profile(validation_state='unvalidated')}):
        with pytest.raises(ValidationError):ServiceGrant(**{**value.model_dump(),**changes})


@pytest.mark.parametrize('changes',[{'attempt_id':uuid4()},{'worker_instance_id':uuid4()}, {'generation':2},
    {'authority_id':uuid4()},{'host_id':uuid4()},{'boot_id':uuid4()},{'epoch':2},{'engine_epoch':uuid4()}])
def test_state_cannot_change_owned_epoch(changes):
    value=grant()
    with pytest.raises(ProtocolError):validate_service_state(value,state(value,**changes))


@pytest.mark.asyncio
async def test_service_transport_signed_claim_and_replayed_uuid(monkeypatch):
    value=grant();body=ServiceClaimBody(protocol_version=2,request_id=uuid4(),service_id=value.profile.service_id,
                                      worker_instance_id=value.ownership.worker_instance_id,host_report=report(value))
    calls=[]
    async def handle(request):
        calls.append(request)
        if len(calls)==1:return httpx.Response(503)
        if request.url.path.endswith('/claim'):return httpx.Response(200,json=sign_service_grant(value,KEY).model_dump(mode='json'))
        return httpx.Response(200,json=state(value,'idle').model_dump(mode='json'))
    async def no_sleep(*_):pass
    monkeypatch.setattr(asyncio,'sleep',no_sleep)
    async with httpx.AsyncClient(base_url='http://synthetic/api/v1',transport=httpx.MockTransport(handle)) as transport:
        client=BackendClient('http://synthetic/api/v1','synthetic',client=transport,max_retries=2)
        assert (await client.service_claim(body,KEY)).grant==value
        assert calls[0].content==calls[1].content
        assert json.loads(calls[0].content)['request_id']==str(body.request_id)
        operation=uuid4()
        assert (await client.service_ready(value,operation,report(value))).state=='idle'
        assert (await client.service_renew(value,uuid4(),report(value))).state=='idle'
        assert (await client.service_status(value)).state=='idle'
    assert [request.url.path for request in calls]==['/api/v1/services/claim']*2+[
        f'/api/v1/services/{value.ownership.attempt_id}/{name}' for name in ('ready','renew','status')]
    assert all(request.method=='POST' for request in calls)
    assert json.loads(calls[2].content)['operation_id']==str(operation)
    assert 'host_report' in json.loads(calls[3].content)
    assert 'operation_id' not in json.loads(calls[4].content)


@pytest.mark.asyncio
async def test_client_rejects_forged_claim_and_foreign_status():
    value=grant();body=ServiceClaimBody(protocol_version=2,request_id=uuid4(),service_id=value.profile.service_id,
                                      worker_instance_id=value.ownership.worker_instance_id,host_report=report(value))
    async with httpx.AsyncClient(base_url='http://synthetic',transport=httpx.MockTransport(lambda request:
        httpx.Response(200,json=sign_service_grant(value,b'forged-key'*4).model_dump(mode='json')))) as transport:
        with pytest.raises(ProtocolError):await BackendClient('http://synthetic','synthetic',client=transport).service_claim(body,KEY)
    async with httpx.AsyncClient(base_url='http://synthetic',transport=httpx.MockTransport(lambda request:
        httpx.Response(200,json=state(value,engine_epoch=uuid4()).model_dump(mode='json')))) as transport:
        with pytest.raises(ProtocolError):await BackendClient('http://synthetic','synthetic',client=transport).service_status(value)


class LeaseClient:
    def __init__(self,value,seconds=10):self.grant=value;self.seconds=seconds;self.phase='warming'
    async def service_status(self,value):return state(value,self.phase,self.seconds)
    async def service_ready(self,value,*_):self.phase='idle';return state(value,self.phase,self.seconds)
    async def service_renew(self,*_):raise ConnectionError('synthetic partition')


def proof(value):
    return CleanupEvidence(attempt_id=value.ownership.attempt_id,boot_id=value.boot_id,processes_stopped=True,
                           models_evicted=True,scratch_cleaned=True,evidence_id='synthetic-owned-cleanup')


def lease(value,client=None,withdraw=None,stop=None,force=None):
    async def read_report():return report(value)
    return ResidentServiceLease(client or LeaseClient(value),value,read_report,withdraw or (lambda:None),
                                stop or (lambda:proof(value)),force or (lambda:None),operation_id_factory=lambda _:uuid4())


@pytest.mark.asyncio
async def test_idle_residency_requires_ready_and_close_preserves_separate_release():
    value=grant();owned=lease(value)
    async with owned:
        owned.require_live()
        with pytest.raises(ProtocolError):owned.require_dispatch()
        await owned.ready(uuid4(),report(value));owned.require_dispatch()
        assert owned.can_dispatch and owned.remaining_seconds>0
        owned.close();assert not owned.can_dispatch
    assert owned.wait_stopped()==proof(value)
    assert value.state=='warming'  # No ledger transition or release claim.
    with pytest.raises(ProtocolError):
        async with owned:pytest.fail('cannot reuse an old lease')


@pytest.mark.asyncio
async def test_partition_expiry_stops_owned_engine_when_event_loop_blocked():
    value=grant();withdrawn=threading.Event();stopped=threading.Event()
    def stop():stopped.set();return proof(value)
    owned=lease(value,LeaseClient(value,.1),withdrawn.set,stop)
    async with owned:
        await owned.ready(uuid4(),report(value))
        assert withdrawn.wait(2) and stopped.wait(2)
        assert not owned.can_dispatch and owned.remaining_seconds==0
    assert owned.wait_stopped()==proof(value)


@pytest.mark.asyncio
async def test_renew_fenced_stops_owned_engine_before_expiry():
    value=grant();client=LeaseClient(value,seconds=10)
    renewed=asyncio.Event();withdrawn=threading.Event();stopped=threading.Event()
    async def renew(*_):
        renewed.set()
        request=httpx.Request('POST','http://synthetic/services/renew')
        raise httpx.HTTPStatusError('fenced',request=request,
            response=httpx.Response(409,json={'code':'attempt_fenced'},request=request))
    def stop():stopped.set();return proof(value)
    client.service_renew=renew
    owned=lease(value,client,withdrawn.set,stop)
    async with owned:
        await owned.ready(uuid4(),report(value))
        assert owned.can_dispatch
        # The unchanged cadence first renews a ten-second lease after about 2.5s.
        await asyncio.wait_for(renewed.wait(),5)
        assert await asyncio.to_thread(stopped.wait,2)
        assert withdrawn.is_set() and not owned.can_dispatch
    assert owned.wait_stopped()==proof(value)


@pytest.mark.asyncio
async def test_renew_stale_report_keeps_dispatch_until_acknowledged_expiry():
    value=grant();client=LeaseClient(value,seconds=10)
    renewed=asyncio.Event();withdrawn=threading.Event();stopped=threading.Event()
    async def renew(*_):
        renewed.set()
        request=httpx.Request('POST','http://synthetic/services/renew')
        raise httpx.HTTPStatusError('stale report',request=request,
            response=httpx.Response(409,json={'code':'hardware_report_stale'},request=request))
    client.service_renew=renew
    owned=lease(value,client,withdrawn.set,stopped.set)
    async with owned:
        await owned.ready(uuid4(),report(value))
        await asyncio.wait_for(renewed.wait(),5)
        await asyncio.sleep(1)
        assert owned.can_dispatch
        assert not withdrawn.is_set() and not stopped.is_set()


@pytest.mark.asyncio
async def test_revocation_is_sticky_and_stalled_cleanup_forces_only_owned_callback():
    value=grant();blocked=threading.Event();forced=threading.Event()
    owned=lease(value,stop=lambda:blocked.wait(2),force=lambda:forced.set())
    async with owned:
        await owned.ready(uuid4(),report(value))
        with pytest.raises(ProtocolError):owned._accept(state(value,'revoked'),__import__('time').monotonic())
        assert not owned.can_dispatch
        with pytest.raises(ProtocolError):owned._accept(state(value,'idle'),__import__('time').monotonic())
        assert forced.wait(2)
        assert owned.wait_stopped() is None
        blocked.set()


@pytest.mark.asyncio
async def test_recovered_resident_cannot_resume():
    value=grant();client=LeaseClient(value);client.phase='idle'
    with pytest.raises(ProtocolError,match='recovered'):
        async with lease(value,client):pytest.fail('old engine cannot resume')


def test_deadline_subtracts_latency_and_cannot_extend_residence(monkeypatch):
    value=grant();owned=lease(value)
    monkeypatch.setattr('task_worker_api.service_execution.time.monotonic',lambda:100)
    owned._accept(state(value,seconds=10),95)
    assert 104.9<owned._deadline<=105
    assert owned._residence<155.1
    with pytest.raises(ProtocolError):owned._accept(state(value,residence_deadline=value.residence_deadline+timedelta(seconds=1)),100)
    assert owned.remaining_seconds==0


def test_partial_or_foreign_callback_is_not_owned_release_proof():
    value=grant()
    for evidence in (proof(value).model_copy(update={'models_evicted':False}),
                     proof(value).model_copy(update={'attempt_id':uuid4()})):
        owned=lease(value,stop=lambda:evidence)
        owned.close()
        assert owned.wait_stopped() is None


@pytest.mark.asyncio
async def test_renew_operation_is_persisted_before_transport():
    value=grant();client=LeaseClient(value);owned=lease(value,client);calls=[];operation=uuid4()
    def persist(kind):calls.append(kind);return operation
    async def renew(received,identity,report):
        assert calls==['renew'] and identity==operation
        owned._active=False
        return state(value)
    client.service_renew=renew;owned.operation_id_factory=persist
    owned._active=True;owned._deadline=__import__('time').monotonic()+.1
    await owned._renew()
    assert calls==['renew']


def test_owned_service_docker_launch_cleanup_and_epoch_fence(tmp_path,monkeypatch):
    value=grant();monkeypatch.setattr('task_worker_api.docker_supervisor.physical_boot_id',lambda *_:value.boot_id)
    supervisor=DockerSupervisor(tmp_path/'private.sqlite',tmp_path/'work',authority_id=value.authority_id,
        host_id=value.host_id,boot_id=value.boot_id,epoch=value.epoch,execution_scope=value.execution_scope,
        cgroup_parent='',network_id='d'*64)
    calls=[];running=[False];container='b'*64
    def docker(*args,**kwargs):
        calls.append(args)
        if args[0]=='create':return container
        if args[0]=='start':running[0]=True
        if args[0] in ('kill','stop'):running[0]=False
        return ''
    monkeypatch.setattr(supervisor,'_docker',docker)
    monkeypatch.setattr(supervisor,'_inspect',lambda *_:{'Id':container,'State':{'Running':running[0],'Pid':100 if running[0] else 0,'OOMKilled':False},
        'NetworkSettings':{'Networks':{'synthetic':{'IPAddress':'172.22.0.4'}}}})
    options=dict(image='sha256:'+'a'*64,command=['synthetic-engine'],read_only_mounts={})
    live=SimpleNamespace(grant=value,require_live=lambda:None)
    supervisor.launch_service(value,live,**options)
    assert supervisor.running_service(value)
    assert supervisor.service_address(value)=='172.22.0.4'
    with pytest.raises(AdmissionError,match='fenced'):supervisor.running_service(value.model_copy(update={'engine_epoch':uuid4()}))
    with pytest.raises(AdmissionError,match='reconciliation'):supervisor.launch_service(value,live,**options)
    result=supervisor.force_stop_service(value)
    assert result.processes_stopped and result.models_evicted and result.scratch_cleaned
    assert ('kill','--signal=KILL',container) in calls
    assert supervisor.sign_cleanup_service(value,KEY).observation.evidence==result
    assert not supervisor.running_service(value)
    assert not (tmp_path/'work'/str(value.ownership.attempt_id)).exists()


def test_prelaunch_proof_requires_recorded_fresh_registration(tmp_path,monkeypatch):
    value=grant();monkeypatch.setattr('task_worker_api.docker_supervisor.physical_boot_id',lambda *_:value.boot_id)
    supervisor=DockerSupervisor(tmp_path/'private.sqlite',tmp_path/'work',authority_id=value.authority_id,
        host_id=value.host_id,boot_id=value.boot_id,epoch=value.epoch,execution_scope=value.execution_scope,
        cgroup_parent='',network_id='d'*64)
    with pytest.raises(AdmissionError,match='launch_unknown'):supervisor.cleanup_service(value)
    supervisor.register_service(value)
    assert not supervisor.running_service(value)
    with pytest.raises(AdmissionError,match='service_not_running'):supervisor.service_address(value)
    assert supervisor.sign_cleanup_service(value,KEY).observation.evidence.processes_stopped
    with pytest.raises(AdmissionError,match='reconciliation'):supervisor.register_service(value)


@pytest.mark.parametrize('refusal',['expired','unsupported'])
def test_refused_service_keeps_registered_prelaunch_proof(tmp_path,monkeypatch,refusal):
    value=grant()
    if refusal=='unsupported':
        value=grant(profile=profile(gpu_count=1,gpu_backend='dx12',gpu_vram_mib=100),gpu_uuid='GPU-'+str(uuid4()))
    else:
        value=value.model_copy(update={'lease_expires_at':datetime.now(timezone.utc)-timedelta(seconds=1)})
    monkeypatch.setattr('task_worker_api.docker_supervisor.physical_boot_id',lambda *_:value.boot_id)
    supervisor=DockerSupervisor(tmp_path/'private.sqlite',tmp_path/'work',authority_id=value.authority_id,
        host_id=value.host_id,boot_id=value.boot_id,epoch=value.epoch,execution_scope=value.execution_scope,
        cgroup_parent='',network_id='d'*64)
    monkeypatch.setattr(supervisor,'_docker',lambda *_a,**_k:pytest.fail('refused service must not launch'))
    live=SimpleNamespace(grant=value,require_live=lambda:None)
    with pytest.raises(AdmissionError):
        supervisor.launch_service(value,live,image='sha256:'+'a'*64,command=['synthetic-engine'],read_only_mounts={})
    assert supervisor._row(value)[1]=='registered'
    assert supervisor.cleanup_service(value).models_evicted
