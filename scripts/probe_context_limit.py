#!/usr/bin/env python3
"""探测 Tabbit 各模型的真实 content 字符上限（上游 492 边界）。

为什么需要：
  claude_compat.py 的 MAX_CONTENT_LEN=18450 是当初拿 "best" 模型实测的通用安全值。
  但 GLM-5.1（20万）、GPT-5.5（1M）等长上下文模型被一刀切砍废。
  本脚本逐模型二分探测真实 492 边界，结果落 config.json，供 claude_compat 按模型分级。

省额度策略：
  492 在上游校验阶段触发（content 太长直接拒收，不生成回答）。
  故用"极短问题 + 长填充"探边界 —— 被拒的请求几乎不烧生成 token。
  每个模型约 12-16 次请求（翻倍试探 + 精细二分），多数被 492 拒。

用法：
  # 探测指定模型（默认 4 个主力）
  python scripts/probe_context_limit.py --token "jwt|next_auth|device_id"

  # 自定义模型列表 + 上限
  python scripts/probe_context_limit.py --token "..." --models "GPT-5.5,GLM-5.1" --max 500000

  # 写入 config.json（默认只打印不写）
  python scripts/probe_context_limit.py --token "..." --write-config

  # 从 config.json 读 token（省得手敲）
  python scripts/probe_context_limit.py --config config.json
"""
import argparse
import asyncio
import json
import logging
import os
import sys

# 让脚本能 import 项目内的 core 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tabbit_client import TabbitClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("probe")

# 大BOSS点名的 4 个主力模型（selected_model 直接用 display_name）
DEFAULT_MODELS = ["Claude-Opus-4.8", "GPT-5.5", "GLM-5.1", "Kimi-K2.6"]

# 探测上限（字符数）。20万字符 ≈ 5万 token，够看 Tabbit 是否限流
DEFAULT_MAX = 200_000

# 二分收敛阈值（字符）。小于此差值即停，够精确
BISECT_EPSILON = 2_000

# 安全余量系数（对齐当初 18450/20500 ≈ 0.9 的做法）
SAFE_FACTOR = 0.9

# 探测用的极短问题，后面拼填充
PROBE_PROMPT = "1+1="


def make_padding_content(target_len: int) -> str:
    """构造指定字符长度的 content：极短问题 + 重复填充。

    填充用无意义文本，避免触发上游任何内容过滤。
    长度精确到 target_len（含 [Assistant]: 后缀的预算由调用方控制）。
    """
    prompt = PROBE_PROMPT
    pad = "x" * max(0, target_len - len(prompt))
    return prompt + pad


def is_492_reject(err: Exception) -> bool:
    """判断异常是否为上游 492（content 过长）拒收"""
    msg = str(err)
    return "492" in msg or "error 492" in msg.lower()


async def probe_one_size(client: TabbitClient, session_id: str, model: str, content: str) -> bool:
    """发一次请求，返回 True=通过校验（能开始流式）, False=被 492 拒收。

    只读首个事件就 break，不读完整个流，省生成 token。
    任何非 492 异常向上抛（可能是 493 版本校验/限流/网络问题，需人工看）。
    """
    try:
        async for evt in client.send_message(session_id, content, model):
            # 收到任意非 error 事件 = 通过校验，上游开始生成
            if evt.get("event") != "error":
                return True
            # error 事件里 code=492 是我们要的信号
            data = evt.get("data", {})
            if str(data.get("code", "")) == "492":
                return False
            # 其他 code 的 error，抛出去给上层判断
            raise Exception(
                f"upstream error {data.get('code')}: {data.get('message')}"
            )
        return True  # 流正常结束也算通过
    except Exception as e:
        if is_492_reject(e):
            return False
        raise


async def probe_model(client: TabbitClient, model: str, max_len: int) -> dict:
    """二分探测单个模型的 492 边界。

    返回:
      {
        "model": model,
        "pass_len": 最后一个通过校验的长度（=真实边界）,
        "fail_len": 第一个被拒的长度,
        "safe_len": pass_len * 0.9（建议落库值）,
        "requests": 总请求数,
        "note": 备注,
      }
    """
    logger.info("▶ 探测模型 %s（上限 %d）", model, max_len)
    session_id = await client.create_chat_session()
    logger.info("  会话 %s", session_id[:8])

    requests = 0
    pass_len = 0
    fail_len = max_len + 1  # 未知上界，先设成 max+1

    # 阶段 1：从已知安全值 18450 起步，翻倍试探找上界
    # （当初 best 实测 20500 ✅，18450 是含余量安全值，作为所有模型的可靠起点）
    lo = 18_450
    hi = max_len
    test = 50_000  # 第一刀直接跳到 5 万，省翻倍次数

    while test <= hi:
        requests += 1
        content = make_padding_content(test)
        try:
            ok = await probe_one_size(client, session_id, model, content)
        except Exception as e:
            logger.warning("  ✗ %s @ %d 抛异常: %s", model, test, e)
            return {
                "model": model,
                "pass_len": pass_len,
                "fail_len": -1,
                "safe_len": int(pass_len * SAFE_FACTOR) if pass_len else 0,
                "requests": requests,
                "note": f"探测中断: {e}",
            }

        if ok:
            logger.info("  ✓ %d 通过", test)
            pass_len = test
            lo = test
            test = min(test * 2, hi)
            if test == lo:
                break  # 到顶了
        else:
            logger.info("  ✗ %d 被拒(492)", test)
            fail_len = test
            hi = test
            break

    # 阶段 2：在 (lo, hi) 之间精细二分
    while hi - lo > BISECT_EPSILON:
        mid = (lo + hi) // 2
        requests += 1
        content = make_padding_content(mid)
        try:
            ok = await probe_one_size(client, session_id, model, content)
        except Exception as e:
            logger.warning("  ✗ %s @ %d 抛异常: %s", model, mid, e)
            break

        if ok:
            logger.info("  ✓ %d 通过 (区间 %d~%d)", mid, lo, hi)
            pass_len = mid
            lo = mid
        else:
            logger.info("  ✗ %d 被拒(492) (区间 %d~%d)", mid, lo, hi)
            fail_len = mid
            hi = mid

    safe_len = int(pass_len * SAFE_FACTOR) if pass_len else 0
    logger.info(
        "◀ %s 探测完成: 边界=%d safe=%d 请求数=%d",
        model, pass_len, safe_len, requests,
    )
    return {
        "model": model,
        "pass_len": pass_len,
        "fail_len": fail_len if fail_len <= max_len else -1,
        "safe_len": safe_len,
        "requests": requests,
        "note": "ok",
    }


async def main():
    ap = argparse.ArgumentParser(description="探测 Tabbit 各模型 content 字符上限")
    ap.add_argument("--token", help='Tabbit token，格式 "jwt|next_auth|device_id"')
    ap.add_argument("--config", help="从 config.json 读 token（与 --token 二选一）")
    ap.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help=f"逗号分隔的模型列表，默认: {','.join(DEFAULT_MODELS)}",
    )
    ap.add_argument("--max", type=int, default=DEFAULT_MAX, help=f"探测上限(字符)，默认 {DEFAULT_MAX}")
    ap.add_argument("--write-config", action="store_true", help="把结果写入 config.json 的 claude.model_context_limits")
    ap.add_argument("--config-path", default="config.json", help="config.json 路径，默认 config.json")
    args = ap.parse_args()

    # 解析 token
    token = args.token
    if not token and args.config:
        with open(args.config) as f:
            cfg = json.load(f)
        tokens = cfg.get("tokens", [])
        if not tokens:
            logger.error("config.json 里没有 tokens")
            sys.exit(1)
        first = tokens[0]
        # config.json 的 tokens 是 dict 数组（token_info），真实 token 在 .value 字段
        # 兼容老格式（裸字符串）
        if isinstance(first, dict):
            token = first.get("value", "")
            if not first.get("enabled", True):
                logger.warning("config.json 第一个 token 是 disabled，仍使用它探测")
        else:
            token = first
        base_url = cfg.get("tabbit", {}).get("base_url", "https://web.tabbit.ai")
        browser_version = cfg.get("tabbit", {}).get("browser_version")
        sparkle_version = cfg.get("tabbit", {}).get("sparkle_version")
    else:
        # 走默认值兜底
        base_url = "https://web.tabbit.ai"
        browser_version = None
        sparkle_version = None

    if not token:
        ap.error("必须提供 --token 或 --config")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    logger.info("待探测模型: %s", models)
    logger.info("探测上限: %d 字符", args.max)

    # 用真实 TabbitClient，复用版本校验/uuid 伪造/时间同步
    client = TabbitClient(
        token,
        base_url=base_url,
        browser_version=browser_version,
        sparkle_version=sparkle_version,
        default_browser=True,
    )

    results = []
    total_requests = 0
    for model in models:
        try:
            r = await probe_model(client, model, args.max)
        except Exception as e:
            logger.error("模型 %s 探测失败: %s", model, e)
            r = {"model": model, "pass_len": 0, "safe_len": 0, "requests": 0, "note": f"失败: {e}"}
        results.append(r)
        total_requests += r.get("requests", 0)

    await client.client.aclose()

    # 汇总
    print("\n" + "=" * 60)
    print("探测结果汇总")
    print("=" * 60)
    print(f"{'模型':<20} {'边界(字符)':<12} {'safe值':<10} {'请求数':<8} 备注")
    print("-" * 60)
    for r in results:
        print(
            f"{r['model']:<20} {r['pass_len']:<12} {r['safe_len']:<10} "
            f"{r['requests']:<8} {r['note']}"
        )
    print("-" * 60)
    print(f"总请求数: {total_requests}")
    print("=" * 60)

    # 可落库的结果（safe_len > 0 的）
    limits = {r["model"]: r["safe_len"] for r in results if r.get("safe_len", 0) > 0}
    print("\n建议落库 (claude.model_context_limits):")
    print(json.dumps(limits, ensure_ascii=False, indent=2))

    if args.write_config:
        config_path = args.config_path
        with open(config_path) as f:
            cfg = json.load(f)
        cfg.setdefault("claude", {}).setdefault("model_context_limits", {}).update(limits)
        with open(config_path, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info("已写入 %s 的 claude.model_context_limits", config_path)
    else:
        logger.info("未指定 --write-config，仅打印不落库")


if __name__ == "__main__":
    asyncio.run(main())
