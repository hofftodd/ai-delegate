from typing import Any

import pytest


def status(lock: dict[str, Any] | None = None, peer_online: bool = True) -> dict[str, Any]:
    return {
        "node": {
            "name": "studio",
            "memory_budget_gb": 220,
            "memory_used_gb": 180,
            "models": [
                {"name": "flint", "state": "ready", "memory_gb": 36, "resident": True},
                {"name": "big", "state": "stopped", "memory_gb": 160, "ctx": 262144},
                {"name": "small", "state": "stopped", "memory_gb": 30, "aliases": ["s"]},
            ],
        },
        "peers": [
            {
                "name": "mini",
                "online": peer_online,
                "node": {
                    "name": "mini",
                    "memory_budget_gb": 32,
                    "memory_used_gb": 30,
                    "lock": lock,
                    "models": [
                        {"name": "flint", "state": "stopped", "memory_gb": 30, "ctx": 262144},
                        {"name": "thinker", "state": "ready", "memory_gb": 30, "ctx": 262144},
                    ],
                },
            }
        ],
    }


def models(peer_online: bool = True) -> dict[str, Any]:
    data = [
        {"id": "flint", "node": "studio"},
        {"id": "big", "node": "studio"},
        {"id": "small", "node": "studio"},
    ]
    if peer_online:
        data += [{"id": "flint@mini", "node": "mini"}, {"id": "thinker", "node": "mini"}]
    return {"data": data}


@pytest.fixture
def cluster():
    from ai_delegate.swap import parse_cluster

    return parse_cluster(status(), models())
