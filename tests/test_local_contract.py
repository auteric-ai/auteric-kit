import asyncio
import json
import sys
from pathlib import Path
from unittest import TestCase
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime/sdk/src'))
from auteric_edge.onboarding import local_check

class LocalContractTests(TestCase):
    def test_static_catalog_uses_valid_sdk_mapping(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve(); (root/'.auteric').mkdir()
            (root/'.auteric/connector.json').write_text(json.dumps({'kind':'catalog','path':'agent-catalog.json'}))
            (root/'agent-catalog.json').write_text(json.dumps({'products':[{'id':'p','title':'Shoe','price':'1.00','currency':'USD'}]}))
            result=asyncio.run(local_check(root,True))
            self.assertEqual(result['status'],'local_contract_passed')
            self.assertEqual(set(result['tested_operations']),{'get_product','search_products'})
            self.assertFalse(result['merchant_writes_executed'])

    def test_local_phase_validates_but_never_executes_mutations(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'.auteric').mkdir()
            (root/'.auteric/connector.json').write_text('{"kind":"factory"}')
            implementation=AsyncMock()
            with patch('auteric_edge.onboarding.connector',return_value=(implementation,[{'operation':'create_cart','kind':'sdk'}],{'create_cart':{'currency':'USD'}})):
                result=asyncio.run(local_check(root,True))
            self.assertEqual(result['status'],'local_contract_passed')
            implementation.execute.assert_not_awaited()
            implementation.close.assert_awaited_once()

    def test_mapping_change_does_not_repin_existing_approval(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'.auteric').mkdir()
            config={'kind':'rest','mappings':[{'operation':'search_products','method':'GET','path':'/products'}],'approved_mapping_digests':['previous']}
            path=root/'.auteric/connector.json';path.write_text(json.dumps(config))
            self.assertEqual(asyncio.run(local_check(root,True))['status'],'mapping_changed')
            self.assertEqual(json.loads(path.read_text()),config)
