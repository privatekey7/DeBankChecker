"""Собирает и проверяет живьём публичные RPC для всех сетей Rabby.

    python tools/build_rpc_registry.py

Источники: chainid.network/chains.json, шаблоны крупных публичных провайдеров
(publicnode, drpc, 1rpc, blastapi, meowrpc, tenderly и др.), ручной список.
Каждый URL проверяется: eth_chainId должен совпасть, eth_getBalance — ответить.
Результат: debank_checker/data/rpc_registry.json — {chain_id: [url, ...]} в
порядке возрастания задержки. Файл включается в поставку; при запуске он
объединяется с живым chainid.network и пользовательским rpc.txt.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import curl_cffi.requests as r

ROOT = Path(__file__).resolve().parent.parent

# Проверенные вручную RPC для сетей, где лежит основная часть балансов —
# идут первыми в списке кандидатов.
PREFERRED_RPC: dict[int, list[str]] = {
    1: ["https://ethereum-rpc.publicnode.com", "https://cloudflare-eth.com", "https://eth.llamarpc.com"],
    10: ["https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"],
    56: ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed1.bnbchain.org", "https://bsc-dataseed2.bnbchain.org"],
    137: ["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"],
    8453: ["https://base-rpc.publicnode.com", "https://mainnet.base.org"],
    42161: ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"],
    43114: ["https://avalanche-c-chain-rpc.publicnode.com", "https://api.avax.network/ext/bc/C/rpc"],
    1868: ["https://rpc.soneium.org", "https://soneium.drpc.org"],
    59144: ["https://linea-rpc.publicnode.com", "https://rpc.linea.build"],
    534352: ["https://scroll-rpc.publicnode.com", "https://rpc.scroll.io"],
    324: ["https://mainnet.era.zksync.io"],
    5000: ["https://mantle-rpc.publicnode.com", "https://rpc.mantle.xyz"],
    100: ["https://gnosis-rpc.publicnode.com", "https://rpc.gnosischain.com"],
    130: ["https://unichain-rpc.publicnode.com", "https://mainnet.unichain.org"],
    57073: ["https://rpc-gel.inkonchain.com", "https://rpc-qnd.inkonchain.com"],
    1135: ["https://rpc.api.lisk.com"],
    34443: ["https://mainnet.mode.network", "https://mode.drpc.org"],
    480: ["https://worldchain-mainnet.g.alchemy.com/public", "https://480.rpc.thirdweb.com"],
    48900: ["https://mainnet.zircuit.com"],
    7777777: ["https://rpc.zora.energy"],
    81457: ["https://rpc.blast.io"],
    167000: ["https://rpc.mainnet.taiko.xyz"],
    204: ["https://opbnb-rpc.publicnode.com", "https://opbnb-mainnet-rpc.bnbchain.org"],
    146: ["https://rpc.soniclabs.com"],
    42220: ["https://forno.celo.org"],
    1116: ["https://rpc.coredao.org"],
    8217: ["https://public-en.node.kaia.io"],
    2222: ["https://evm.kava.io"],
    169: ["https://pacific-rpc.manta.network/http"],
    1625: ["https://rpc.gravity.xyz"],
    20240603: ["https://rpc.dbkchain.io"],
    4663: ["https://rpc.mainnet.chain.robinhood.com", "https://robinhood-rpc.publicnode.com"],
    143: ["https://rpc.monad.xyz"],
    999: ["https://rpc.hyperliquid.xyz/evm"],
}

RABBY_CHAINS = json.load(open("/tmp/rabby_chains.json")) if Path("/tmp/rabby_chains.json").exists() else None
TEST_ADDR = "0x0000000000000000000000000000000000000001"

# слаги провайдеров по chain_id (только там, где известны)
SLUGS = {
    1: ["ethereum", "eth", "mainnet"], 10: ["optimism", "op"], 56: ["bsc", "bnb"], 100: ["gnosis", "xdai"],
    137: ["polygon", "matic"], 8453: ["base"], 42161: ["arbitrum", "arbitrum-one", "arb"], 43114: ["avalanche", "avax"],
    59144: ["linea"], 534352: ["scroll"], 324: ["zksync", "zksync-era"], 5000: ["mantle"], 130: ["unichain"],
    57073: ["ink"], 1135: ["lisk"], 34443: ["mode"], 480: ["worldchain", "world-chain"], 48900: ["zircuit"],
    7777777: ["zora"], 81457: ["blast"], 167000: ["taiko"], 204: ["opbnb"], 146: ["sonic"], 42220: ["celo"],
    1116: ["core"], 8217: ["kaia", "klaytn"], 2222: ["kava"], 169: ["manta", "manta-pacific"], 1625: ["gravity"],
    1868: ["soneium"], 252: ["fraxtal"], 33139: ["apechain"], 1088: ["metis"], 2818: ["morph"], 80094: ["berachain", "bera"],
    1329: ["sei"], 7000: ["zetachain", "zeta"], 1030: ["conflux"], 7560: ["cyber"], 143: ["monad"], 2020: ["ronin"],
    13371: ["immutable", "immutable-zkevm"], 122: ["fuse"], 4200: ["merlin"], 30: ["rootstock", "rsk"], 50104: ["sophon"],
    200901: ["bitlayer"], 60808: ["bob"], 25: ["cronos"], 999: ["hyperliquid", "hyperevm"], 88888: ["chiliz"],
    2741: ["abstract"], 14: ["flare"], 196: ["xlayer", "x-layer"], 1514: ["story"], 232: ["lens"], 747474: ["katana"],
    98866: ["plume"], 43111: ["hemi"], 50: ["xdc"], 1111: ["wemix"], 9745: ["plasma"], 42793: ["etherlink"],
    16661: ["0g"], 223: ["b2", "bsquared"], 4663: ["robinhood"], 20240603: ["dbk", "dbkchain"], 988: ["stable"],
    4326: ["megaeth"], 4217: ["tempo"], 2366: ["kite"], 5042: ["arc"], 4114: ["citrea"],
}
TEMPLATES = [
    "https://{s}-rpc.publicnode.com", "https://{s}.drpc.org", "https://1rpc.io/{s}", "https://{s}-mainnet.public.blastapi.io",
    "https://{s}.meowrpc.com", "https://{s}.gateway.tenderly.co", "https://rpc.{s}.gateway.fm", "https://{s}.rpc.thirdweb.com",
    "https://{s}.llamarpc.com", "https://{s}.blockpi.network/v1/rpc/public", "https://rpc-{s}.publicnode.com",
    "https://{s}-mainnet.g.alchemy.com/v2/demo", "https://{s}.api.onfinality.io/public", "https://{s}.rpc.subquery.network/public",
    "https://endpoints.omniatech.io/v1/{s}/mainnet/public", "https://rpc.ankr.com/{s}",
]
EXTRA = {
    2741: ["https://api.mainnet.abs.xyz"], 999: ["https://rpc.hyperliquid.xyz/evm", "https://rpc.hyperlend.finance", "https://hyperliquid.drpc.org"],
    9745: ["https://rpc.plasma.to"], 4326: ["https://mainnet.megaeth.com/rpc", "https://rpc.megaeth.com"], 988: ["https://rpc.stable.xyz"],
    4217: ["https://rpc.tempo.xyz", "https://rpc.mainnet.tempo.xyz"], 5042: ["https://rpc.arc.network", "https://rpc.mainnet.arc.network"],
    2366: ["https://rpc.gokite.ai", "https://rpc.kite.ai"], 4114: ["https://rpc.mainnet.citrea.xyz", "https://rpc.citrea.xyz"],
    16661: ["https://evmrpc.0g.ai", "https://evmrpc-mainnet.0g.ai"], 1514: ["https://mainnet.storyrpc.io"], 232: ["https://rpc.lens.xyz"],
    747474: ["https://rpc.katana.network", "https://rpc.katanarpc.com"], 98866: ["https://rpc.plume.org", "https://phoenix-rpc.plumenetwork.xyz"],
    43111: ["https://rpc.hemi.network/rpc"], 42793: ["https://node.mainnet.etherlink.com"], 223: ["https://rpc.bsquared.network", "https://mainnet.b2-rpc.com"],
    4663: ["https://rpc.mainnet.chain.robinhood.com", "https://robinhood-rpc.publicnode.com"], 20240603: ["https://rpc.dbkchain.io", "https://rpc.mainnet.dbkchain.io"],
    2818: ["https://rpc.morphl2.io", "https://rpc-quicknode.morphl2.io"], 80094: ["https://rpc.berachain.com"], 1329: ["https://evm-rpc.sei-apis.com"],
    7000: ["https://zetachain-evm.blockpi.network/v1/rpc/public", "https://zetachain-mainnet.g.allthatnode.com/archive/evm"],
    1030: ["https://evm.confluxrpc.com"], 7560: ["https://cyber.alt.technology", "https://rpc.cyber.co"], 2020: ["https://api.roninchain.com/rpc"],
    13371: ["https://rpc.immutable.com"], 122: ["https://rpc.fuse.io"], 4200: ["https://rpc.merlinchain.io"], 30: ["https://public-node.rsk.co"],
    50104: ["https://rpc.sophon.xyz"], 200901: ["https://rpc.bitlayer.org", "https://rpc.bitlayer-rpc.com"], 60808: ["https://rpc.gobob.xyz"],
    25: ["https://evm.cronos.org", "https://cronos-evm-rpc.publicnode.com"], 88888: ["https://rpc.ankr.com/chiliz", "https://chiliz.publicnode.com"],
    14: ["https://flare-api.flare.network/ext/C/rpc"], 196: ["https://rpc.xlayer.tech", "https://xlayerrpc.okx.com"], 50: ["https://erpc.xinfin.network", "https://rpc.xdc.org"],
    1111: ["https://api.wemix.com"], 33139: ["https://rpc.apechain.com", "https://apechain.calderachain.xyz/http"], 1088: ["https://andromeda.metis.io/?owner=1088"],
    252: ["https://rpc.frax.com"], 143: ["https://rpc.monad.xyz"],
}


def load_chainid() -> dict[int, list[str]]:
    data = r.get("https://chainid.network/chains.json", timeout=30).json()
    out: dict[int, list[str]] = {}
    for c in data:
        urls = [u for u in c.get("rpc", []) if isinstance(u, str) and u.startswith("https://") and "${" not in u]
        if urls:
            out[int(c["chainId"])] = urls
    return out


def probe(chain_id: int, url: str) -> tuple[str, float] | None:
    t = time.time()
    try:
        resp = r.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=8)
        if resp.status_code != 200:
            return None
        cid = int(resp.json()["result"], 16)
        if cid != chain_id:
            return None
        resp = r.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "eth_getBalance", "params": [TEST_ADDR, "latest"]}, timeout=8)
        if resp.status_code != 200 or "result" not in resp.json():
            return None
        return url, time.time() - t
    except Exception:
        return None


def main() -> None:
    chains = RABBY_CHAINS or []
    ids = {int(c["community_id"]): c["id"] for c in chains} if chains else {cid: str(cid) for cid in PREFERRED_RPC}
    registry = load_chainid()
    cand: dict[int, list[str]] = {}
    for cid in ids:
        urls: list[str] = []
        for u in PREFERRED_RPC.get(cid, []) + EXTRA.get(cid, []) + registry.get(cid, []):
            if u not in urls:
                urls.append(u)
        for slug in SLUGS.get(cid, []):
            for t in TEMPLATES:
                u = t.format(s=slug)
                if u not in urls:
                    urls.append(u)
        cand[cid] = urls
    total = sum(len(v) for v in cand.values())
    print(f"chains={len(cand)} candidates={total}")
    jobs = [(cid, u) for cid, us in cand.items() for u in us]
    with ThreadPoolExecutor(max_workers=48) as ex:
        res = list(ex.map(lambda j: (j[0], probe(*j)), jobs))
    good: dict[int, list[tuple[str, float]]] = {}
    for cid, ok in res:
        if ok:
            good.setdefault(cid, []).append(ok)
    out = {str(cid): [u for u, _ in sorted(v, key=lambda x: x[1])] for cid, v in good.items()}
    (ROOT / "debank_checker" / "data").mkdir(exist_ok=True)
    json.dump(out, open(ROOT / "debank_checker" / "data" / "rpc_registry.json", "w"), indent=1)
    for cid, slug in sorted(ids.items(), key=lambda kv: len(good.get(kv[0], []))):
        print(f"{slug:9} {cid:>9} working={len(good.get(cid, [])):3} / {len(cand[cid])}")
    print("chains without RPC:", [ids[c] for c in ids if c not in good])


if __name__ == "__main__":
    main()
