"""Owner recovery lists existing machines without running, creating or resuming one."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from auth import OAuth, Settings, OAuthError
from machine import Machine


def setup(tmp_path, items, remaining=False):
    oauth = OAuth(Settings('https://example.up.railway.app','p'*30,'s'*42,str(tmp_path/'auth.sqlite')))
    paginator = SimpleNamespace(has_next=True)
    def next_page():
        paginator.has_next = remaining
        return items
    paginator.next_items = Mock(side_effect=next_page)
    factory = Mock()
    factory.list.return_value = paginator
    factory.create.side_effect = AssertionError('Recovery must never create')
    factory.connect.side_effect = AssertionError('Recovery must never connect or resume')
    return Machine(oauth, factory), oauth, factory, paginator


def item(sid, state='running', metadata=None):
    return SimpleNamespace(sandbox_id=sid,state=state,metadata=metadata or {},
                           started_at=datetime(2026,11,6,tzinfo=timezone.utc))


def test_recovery_returns_fresh_signed_handles_without_execution(tmp_path):
    machine, oauth, factory, pages = setup(tmp_path, [item('legacy-a'), item('paused-b','paused')])
    result = machine.status('recover')
    assert result['created_workspace'] is False
    assert len(result['workspaces']) == 2
    for row in result['workspaces']:
        assert oauth.verify(row['workspace_id'],'workspace')['sandbox_id'] == row['sandbox_id']
        assert row['legacy_untagged'] is True
    factory.create.assert_not_called()
    factory.connect.assert_not_called()


def test_recovery_excludes_other_installations_and_ended_sandboxes(tmp_path):
    machine, oauth, factory, pages = setup(tmp_path, [])
    pages.next_items.side_effect = None
    pages.next_items.return_value = [item('ours',metadata={'dev_mcp_issuer':machine.issuer}),
                                    item('other',metadata={'dev_mcp_issuer':'foreign-instance'}),
                                    item('ended','killed'), item('ours',metadata={'dev_mcp_issuer':machine.issuer})]
    pages.has_next = True
    result = machine.recover()
    assert [r['sandbox_id'] for r in result['workspaces']] == ['ours']
    assert result['workspaces'][0]['legacy_untagged'] is False


def test_empty_inventory_does_not_replace_lost_workspace(tmp_path):
    machine, oauth, factory, pages = setup(tmp_path, [])
    result = machine.recover()
    assert result['workspaces'] == []
    assert result['created_workspace'] is False
    factory.create.assert_not_called()


def test_recovery_scan_is_bounded(tmp_path):
    machine, oauth, factory, pages = setup(tmp_path, [item('a')], remaining=True)
    result = machine.recover()
    assert pages.next_items.call_count == 2
    assert result['truncated'] is True
    assert len(result['workspaces']) == 1


def test_recovery_keyword_cannot_authorize_terminal_or_deletion(tmp_path):
    machine, oauth, factory, pages = setup(tmp_path, [])
    for operation in (lambda:machine.run('recover','id'),lambda:machine.close('recover'),
                      lambda:machine.read('recover','notes.txt')):
        with pytest.raises(OAuthError): operation()
    factory.connect.assert_not_called()


def test_raw_sandbox_id_cannot_be_used_as_signed_handle(tmp_path):
    machine, oauth, factory, pages = setup(tmp_path, [item('raw-sandbox-id')])
    with pytest.raises(OAuthError): machine.status('raw-sandbox-id')
    factory.connect.assert_not_called()


def test_inventory_failure_is_not_reported_as_empty_or_replaced(tmp_path):
    machine, oauth, factory, pages = setup(tmp_path, [])
    pages.next_items.side_effect = RuntimeError('E2B API unavailable')
    with pytest.raises(RuntimeError,match='unavailable'): machine.recover()
    factory.create.assert_not_called()
