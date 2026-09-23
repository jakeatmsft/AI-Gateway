"""Deployment recovery tests; all Azure calls are mocked."""

import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

from src import lab


def record(deployment_id, status=4, active=True):
    return {"id": deployment_id, "status": status, "complete": status in (3, 4), "active": active}


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.scope = ("--subscription", "test", "--resource-group", "test", "--name", "test")
        self.output = patch('sys.stdout', new=io.StringIO())
        self.output.start()
        redactor = patch.object(lab, "_redactor_ready", return_value=True)
        redactor.start()
        self.addCleanup(redactor.stop)
        self.addCleanup(self.output.stop)

    def publish(self, records, upload_error=None):
        upload = Mock()
        snapshots = iter(records)

        def azure(*args, **kwargs):
            if args[:4] == ("functionapp", "log", "deployment", "list"):
                value = next(snapshots)
                if isinstance(value, Exception):
                    raise value
                return value
            if args[:4] == ("functionapp", "deployment", "source", "config-zip"):
                upload()
                if upload_error:
                    raise upload_error
                return record("new")
            raise AssertionError(args)

        with patch.object(lab, 'az_json', side_effect=azure), patch.object(lab.time, 'sleep'):
            result = lab._publish_function_package('test.zip', self.scope)
        upload.assert_called_once()
        return result

    def test_reset_while_polling_resumes_without_reupload(self):
        reset = RuntimeError('ConnectionResetError(10054, Connection aborted)')
        result = self.publish([[record('old')], reset, [record('old'), record('new', 1)],
                               [record('old'), record('new')]], reset)
        self.assertEqual(result['id'], 'new')

    def test_success_is_checked_against_new_deployment(self):
        result = self.publish([[record('old')], [record('old'), record('new')]])
        self.assertEqual(result['id'], 'new')

    def test_server_build_failure_is_not_hidden(self):
        with self.assertRaisesRegex(RuntimeError, 'deployment new failed'):
            self.publish([[record('old')], [record('new', 3)]], RuntimeError('Read timed out'))

    def test_previous_success_is_not_accepted(self):
        with patch.object(lab, '_function_deployment_records', return_value=[record('old')]), \
             patch.object(lab.time, 'monotonic', side_effect=[0, 0, 0, 61]):
            with self.assertRaisesRegex(RuntimeError, 'Could not identify'):
                lab._wait_for_function_deployment(self.scope, previous_ids={'old'})

    def test_concurrent_publications_are_ambiguous(self):
        with patch.object(lab, '_function_deployment_records', return_value=[record('new1'), record('new2')]):
            with self.assertRaisesRegex(RuntimeError, 'Multiple new'):
                lab._wait_for_function_deployment(self.scope, previous_ids={'old'})

    def test_explicit_resume_uses_requested_active_deployment(self):
        with patch.object(lab, '_function_deployment_records', return_value=[record('new'), record('old', active=False)]):
            self.assertEqual(lab._wait_for_function_deployment(self.scope, deployment_id='new')['id'], 'new')
            with self.assertRaisesRegex(RuntimeError, 'superseded'):
                lab._wait_for_function_deployment(self.scope, deployment_id='old')

    def test_resume_finishes_apim_without_republishing(self):
        outputs = {'proxyAppName': 'test', 'proxyUrl': 'https://test.invalid'}
        prepared = {'subscription': 'test', 'resource_group': 'test',
                    'backend_url': 'https://test.openai.azure.com/openai/v1',
                    'parameters': {'apiName': 'test', 'apiPath': 'test', 'apimServiceName': 'test',
                                   'backendId': '', 'backendResponsesPath': '/responses'}}
        final = {'properties': {'provisioningState': 'Succeeded'}}

        def azure(*args, **kwargs):
            if args[:3] == ('deployment', 'group', 'show'):
                raise RuntimeError('DeploymentNotFound')
            if args[:3] == ('deployment', 'group', 'create'):
                if args[args.index('--name') + 1].endswith('-proxy-function'):
                    return {'properties': {'outputs': {key: {'value': value} for key, value in outputs.items()}}}
                return final
            if args[:2] in (('functionapp', 'stop'), ('functionapp', 'start')):
                return {}
            raise AssertionError(args)

        ready = Mock(status_code=400)
        ready.json.return_value = {'error': 'Expected a streaming or web_search Responses request with valid blocked domains'}
        with patch.object(lab, 'az_json', side_effect=azure), \
             patch.object(lab, 'grant_backend_access'), \
             patch.object(lab, '_wait_for_function_deployment') as wait, \
             patch.object(lab, '_publish_function_package') as publish, \
             patch.object(lab.requests, 'post', return_value=ready):
            self.assertEqual(lab.deploy({'AZURE_OPENAI_API_KEY': 'test'}, prepared,
                                       resume_proxy_deployment_id='new'), final)
            wait.assert_called_once_with(self.scope, deployment_id='new')
            publish.assert_not_called()

    def test_reuse_updates_apim_with_existing_proxy_key_without_republishing(self):
        outputs = {'proxyAppName': 'test', 'proxyUrl': 'https://test.invalid'}
        prepared = {'subscription': 'test', 'resource_group': 'test',
                    'backend_url': 'https://test.openai.azure.com/openai/v1',
                    'parameters': {'apiName': 'test', 'apiPath': 'test', 'apimServiceName': 'test',
                                   'backendId': '', 'backendResponsesPath': '/responses'}}
        final = {'properties': {'provisioningState': 'Succeeded'}}

        def azure(*args, **kwargs):
            if args[:3] == ('deployment', 'group', 'show'):
                return {'properties': {'outputs': {key: {'value': value} for key, value in outputs.items()}}}
            if args[:4] == ('functionapp', 'config', 'appsettings', 'list'):
                return [{'name': 'PROXY_API_KEY', 'value': 'existing-proxy-key'}]
            if args[:3] == ('deployment', 'group', 'create'):
                self.assertEqual(args[args.index('--name') + 1], 'test')
                params = json.loads(Path(args[args.index('--parameters') + 1][1:]).read_text())['parameters']
                self.assertEqual(params['streamingProxyUrl']['value'], outputs['proxyUrl'])
                self.assertEqual(params['streamingProxyKey']['value'], 'existing-proxy-key')
                return final
            raise AssertionError(args)  # No Function provisioning, publishing, or restart.

        ready = Mock(status_code=400)
        ready.json.return_value = {'error': 'Expected a streaming or web_search Responses request with valid blocked domains'}
        with patch.object(lab, 'az_json', side_effect=azure), \
             patch.object(lab, 'grant_backend_access'), \
             patch.object(lab, '_publish_function_package') as publish, \
             patch.object(lab.requests, 'post', return_value=ready) as post:
            self.assertEqual(lab.deploy({}, prepared, reuse_streaming_proxy=True), final)
            publish.assert_not_called()
            self.assertEqual(post.call_args.kwargs['headers']['x-proxy-key'], 'existing-proxy-key')
            UUID(post.call_args.kwargs['headers']['x-response-request-id'])
            self.assertEqual(post.call_args.kwargs['json'], {})  # Never invoke a model during readiness.
        self.assertNotIn('streamingProxyKey', prepared['parameters'])

    def test_readiness_rejects_old_header_contract_and_authentication_errors(self):
        for status, error in ((400, 'Missing gateway request ID'), (401, 'Invalid proxy credential')):
            response = Mock(status_code=status)
            response.json.return_value = {'error': error}
            with self.subTest(status=status), patch.object(lab.requests, 'post', return_value=response):
                self.assertFalse(lab._streaming_proxy_ready('https://test.invalid', 'key'))

    def test_reuse_requires_existing_ready_proxy(self):
        prepared = {'subscription': 'test', 'resource_group': 'test',
                    'backend_url': 'https://test.openai.azure.com/openai/v1',
                    'parameters': {'apiName': 'test', 'backendId': '', 'backendResponsesPath': '/responses'}}
        outputs = {'proxyAppName': 'test', 'proxyUrl': 'https://test.invalid'}
        previous = {'properties': {'outputs': {key: {'value': value} for key, value in outputs.items()}}}
        scenarios = (
            ([RuntimeError('DeploymentNotFound')], 'No existing streaming proxy'),
            ([previous, []], 'No existing streaming proxy'),
            ([previous, [{'name': 'PROXY_API_KEY', 'value': 'key'}]], 'not ready for x-response-request-id'),
        )
        for results, message in scenarios:
            with self.subTest(message=message), \
                 patch.object(lab, 'az_json', side_effect=results), \
                 patch.object(lab, 'grant_backend_access'), \
                 patch.object(lab, '_streaming_proxy_ready', return_value=False):
                with self.assertRaisesRegex(RuntimeError, message):
                    lab.deploy({}, prepared, reuse_streaming_proxy=True)

    def test_reuse_conflicting_options_fail_before_azure_changes(self):
        with patch.object(lab, 'grant_backend_access') as grant:
            with self.assertRaises(ValueError):
                lab.deploy({}, {}, reuse_streaming_proxy=True, resume_proxy_deployment_id='new')
            with self.assertRaises(ValueError):
                lab.deploy({'ENABLE_STREAMING_PROXY': 'false'}, {}, reuse_streaming_proxy=True)
            grant.assert_not_called()

    def test_permission_error_is_not_retried(self):
        with patch.object(lab, 'az_json', side_effect=RuntimeError('403 Forbidden')) as azure:
            with self.assertRaisesRegex(RuntimeError, 'Forbidden'):
                lab._function_deployment_records(self.scope)
            azure.assert_called_once()

    def test_cli_timeout_does_not_expose_command_arguments(self):
        with patch.object(lab, 'azure_cli_command', return_value=['az']), \
             patch.object(lab.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['az', 'secret'], 45)):
            with self.assertRaisesRegex(RuntimeError, 'request timed out after 45 seconds') as error:
                lab.az_json('account', 'show', timeout=45)
            self.assertNotIn('secret', str(error.exception))
