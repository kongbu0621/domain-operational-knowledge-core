"""Scope boundaries through public K APIs; all records are synthetic."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from domain_operational_knowledge_core import KnowledgeError, open_store
from helpers import setup, encode, request, material, proposal, review
from test_lifecycle import adoption, completion, retirement


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = setup(self.root)
        self.scope = 's' * 256

    def run_op(self, operation, body, key=None):
        return self.store.execute(encode(request(operation, body, key or 'request:' + operation, self.scope)))

    def hashes(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                (self.root/'knowledge/knowledge.sqlite', self.root/'materials.sqlite')}

    def test_long_scope_full_lifecycle_and_receipt_replay(self):
        self.run_op('publish_material', material())
        proposed = self.run_op('propose', proposal())['receipt']['result']
        version = self.run_op('review', review(proposed))['receipt']['result']['version_id']
        used = self.run_op('start_use', adoption(version))
        first = self.run_op('complete_use', completion())
        self.assertEqual(self.store.get('usage', 'usage:build')['record']['scope'], self.scope)
        self.assertEqual(self.store.get('feedback', first['receipt']['result']['feedback_id'])['record']['scope'], self.scope)
        correction = dict(completion(material_id='material:corrected'), expected_feedback_id=first['receipt']['result']['feedback_id'])
        corrected = self.run_op('correct_feedback', correction)
        revision = dict(proposal('proposal:revision'), expected_version_id=version, material_ids=['material:corrected'])
        next_candidate = self.run_op('propose_revision', revision)['receipt']['result']
        next_version = self.run_op('review', dict(review(next_candidate), evidence_material_ids=['material:corrected']),
                                   'request:revision-review')['receipt']['result']['version_id']
        self.run_op('retire', retirement(next_version, 'material:corrected'))
        self.store = open_store(self.root/'knowledge')
        before = self.hashes()
        self.assertEqual(self.run_op('start_use', adoption(version))['receipt'], used['receipt'])
        self.assertEqual(self.run_op('correct_feedback', correction)['receipt'], corrected['receipt'])
        self.assertEqual(self.store.get('version', version)['record']['state'], 'SUPERSEDED')
        self.assertEqual(self.store.get('version', next_version)['record']['state'], 'RETIRED')
        self.assertEqual(self.hashes(), before)

    def test_scope_suffix_is_part_of_request_and_candidate_identity(self):
        self.run_op('publish_material', material())
        result = []
        for scope in ('s' * 255, 's' * 255 + 'a', 's' * 255 + 'b'):
            value = request('propose', proposal(), 'request:same', scope)
            receipt = self.store.execute(encode(value))['receipt']
            self.assertEqual(receipt, self.store.get_operation(scope, 'propose', 'request:same')['receipt'])
            self.assertEqual(receipt, open_store(self.root/'knowledge').execute(encode(value))['receipt'])
            result.append(receipt['result']['candidate_id'])
        self.assertEqual(len(set(result)), 3)

    def test_pending_publication_resumes_with_full_scope(self):
        original = self.store.ledger.publish
        def lost(*args):
            original(*args)
            raise KnowledgeError('IO_ERROR', 'Synthetic lost Ledger response', 'unknown')
        with patch.object(self.store.ledger, 'publish', side_effect=lost):
            with self.assertRaises(KnowledgeError):
                self.run_op('publish_material', material())
        reopened = open_store(self.root/'knowledge')
        self.assertEqual(reopened.pending(self.scope, 'request:publish_material')['state'], 'PENDING')
        result = reopened.resume(self.scope, 'request:publish_material')
        before = self.hashes()
        self.assertEqual(reopened.resume(self.scope, 'request:publish_material')['receipt'], result['receipt'])
        self.assertEqual(self.hashes(), before)
        command = [sys.executable, '-m', 'domain_operational_knowledge_core', '--store', str(self.root/'knowledge'),
                   'operation', '--scope', self.scope, '--operation', 'publish_material', '--request-id', 'request:publish_material']
        cli = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)['data']['receipt'], result['receipt'])

    def test_utf8_scope_retains_byte_based_limit(self):
        self.scope = '界' * 85 + 'x'
        self.assertEqual(len(self.scope.encode()), 256)
        saved = self.run_op('publish_material', material())
        reopened = open_store(self.root/'knowledge')
        self.assertEqual(reopened.get_operation(self.scope, 'publish_material', 'request:publish_material')['receipt'], saved['receipt'])
        read = reopened.read_material('material:build')
        self.assertEqual(base64.b64decode(read['entries'][0]['data']), material()['text'].encode())

    def test_oversized_scope_and_unchanged_id_limits_fail_without_writes(self):
        before = self.hashes()
        for scope in ('s' * 257, '界' * 85 + 'xx', '', 's\x00'):
            for call in (lambda: self.store.execute(encode(request('publish_material', material(), scope=scope))),
                         lambda: self.store.get_operation(scope, 'publish_material', 'request:one'),
                         lambda: self.store.pending(scope, 'request:one'),
                         lambda: self.store.resume(scope, 'request:one')):
                with self.subTest(scope_bytes=len(scope.encode())):
                    with self.assertRaises(KnowledgeError) as error:
                        call()
                    self.assertEqual(error.exception.code, 'INVALID_INPUT')
                    self.assertEqual(self.hashes(), before)
        for value in (request('publish_material', material(), 'r' * 129),
                      request('publish_material', material('m' * 129))):
            with self.assertRaises(KnowledgeError) as error:
                self.store.execute(encode(value))
            self.assertEqual(error.exception.code, 'INVALID_INPUT')
            self.assertEqual(self.hashes(), before)
