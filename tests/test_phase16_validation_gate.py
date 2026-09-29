from types import SimpleNamespace, ModuleType
import sys

from validation.opportunity import OpportunityValidator
from logger import Opportunity, OppType


class FakeFn:
    def __init__(self, result=None, fail=False):
        self.result = result
        self.fail = fail

    def call(self, block_identifier=None):
        if self.fail:
            raise RuntimeError("quote failed")
        return self.result

    def build_transaction(self, tx):
        return {"to": "0x" + "11" * 20, "data": "0x1234", **tx}


class FakeFunctions:
    def __init__(self, ad, profit, fail_call=False):
        self.ad = ad
        self.profit = profit
        self.fail_call = fail_call

    def yieldOut(self, raw):
        return FakeFn((self.ad, self.profit), self.fail_call)

    def swap(self, *args):
        return FakeFn()


class FakeContract:
    def __init__(self, ad, profit):
        self.functions = FakeFunctions(ad, profit)


class FakeEth:
    gas_price = 1
    block_number = 100

    def get_block(self, tag):
        return {"number": 100}

    def estimate_gas(self, tx, block_identifier=None):
        return 210_000

    def call(self, tx, block_identifier=None):
        return b""


class FakeW3:
    eth = FakeEth()

    @staticmethod
    def to_checksum_address(x):
        return x


def test_execution_validation_requires_full_evm_simulation(monkeypatch):
    validator = object.__new__(OpportunityValidator)
    validator.w3 = FakeW3()
    validator.chain_id = 1
    validator.address = "0x" + "11" * 20
    ad = (100, 0, ["0x"+"01"*20, "0x"+"02"*20, "0x"+"01"*20], ["0x"+"03"*20, "0x"+"04"*20], [3000,3000], "0x"+"01"*20, 0)
    validator.contract = FakeContract(ad, 1000)
    fake_signer = ModuleType("executor.signer")
    fake_signer.get_sender = lambda: "0x" + "aa" * 20
    monkeypatch.setitem(sys.modules, "executor.signer", fake_signer)

    opp = Opportunity(
        opp_type=OppType.DEX_SPREAD,
        chain_id=1,
        block_number=100,
        tokens=ad[2], pools=ad[3], dexes=["UniswapV2", "UniswapV3"],
        metadata={"execution_supported": True, "fees_pip": [3000,3000]},
    )
    result = validator.validate(opp, block_number=100)
    assert result.ok
    assert result.status == "execution_simulated"
    assert result.quote_profit_wei == 1000
    assert result.validated_floor_wei == 900
    assert result.gas_estimate == 210_000
