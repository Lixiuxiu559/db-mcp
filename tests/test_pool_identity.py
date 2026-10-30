"""缓存连接必须与本次传入的实例配置一致，包含实际密码。"""

import pytest
from pydantic import SecretStr


@pytest.mark.parametrize(
    ("instance", "method", "cache", "signatures"),
    [
        ("pg-read", "get_pg_pool", "_pg", "_pg_signatures"),
        ("redis-read", "get_redis_client", "_redis", "_redis_signatures"),
    ],
)
async def test_cached_other_target_is_replaced_for_waiting_request(
    gw, harness, instance, method, cache, signatures
):
    pool = gw.state.pool
    old = gw.state.config.get_instance(instance)
    changed = old.model_copy(update={"host": "10.0.0.99", "password": SecretStr("new-password")})
    get = getattr(pool, method)
    # 新配置 B 已缓存；携带旧配置 A 的请求随后才结束排队。
    cached_b = await get(instance, changed)
    result_a = await get(instance, old)
    assert result_a is not cached_b
    assert harness.connect_calls[-1]["host"] == old.host
    assert harness.connect_calls[-1]["password"] == old.password.get_secret_value()
    assert getattr(pool, signatures)[instance] == old.model_dump()
    # 同一配置继续复用，不做无意义重连。
    assert await get(instance, old) is result_a
    assert len(harness.connect_calls) == 2
    await pool.invalidate(instance)
    assert instance not in getattr(pool, cache)
    assert instance not in getattr(pool, signatures)


@pytest.mark.parametrize(
    ("instance", "method", "signatures"),
    [
        ("pg-read", "get_pg_pool", "_pg_signatures"),
        ("redis-read", "get_redis_client", "_redis_signatures"),
    ],
)
async def test_password_only_change_is_part_of_connection_signature(
    gw, harness, instance, method, signatures
):
    pool = gw.state.pool
    old = gw.state.config.get_instance(instance)
    changed = old.model_copy(update={"password": SecretStr("new-password")})
    # JSON 序列化都只给掩码，不能用于判断连接是否相同。
    assert old.model_dump_json() == changed.model_dump_json()
    assert old.model_dump() != changed.model_dump()
    get = getattr(pool, method)
    cached = await get(instance, old)
    replaced = await get(instance, changed)
    assert replaced is not cached
    assert harness.connect_calls[-1]["password"] == "new-password"
    assert len(harness.connect_calls) == 2
    await pool.close_all()
    assert getattr(pool, signatures) == {}
