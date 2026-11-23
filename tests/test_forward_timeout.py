"""折扇：合并转发可配置超时 + 超时降级（v1.1.1）。

## 背景

框架 `send_action` 默认只等 10 秒回包，而合并转发要先打包上传节点内容，
经常超时。超时后 NapCat 不会撤销已受理的请求（卡片照样投递，只是回包迟到），
旧逻辑把超时当失败去降级重发，于是群里出现两份内容。

## 本版改动

1. 新增 `forward_timeout`（默认 60 秒，0=框架默认）：显式拉长转发等待，
   从源头减少超时；
2. 超时后仍走**原有**降级逻辑（宁可不吞消息）——降级只针对「确实还没发」的部分。

## 运行

    python3 tests/test_forward_timeout.py
"""
import asyncio
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── 桩 core.* ─────────────────────────────────────────────────
core = types.ModuleType("core"); sys.modules["core"] = core
plugin_mod = types.ModuleType("core.plugin"); sys.modules["core.plugin"] = plugin_mod
chat_mod = types.ModuleType("core.chat"); sys.modules["core.chat"] = chat_mod
mu_mod = types.ModuleType("core.chat.message_utils"); sys.modules["core.chat.message_utils"] = mu_mod
me_mod = types.ModuleType("core.chat.message_elements"); sys.modules["core.chat.message_elements"] = me_mod
ph_mod = types.ModuleType("core.plugin.plugin_handlers"); sys.modules["core.plugin.plugin_handlers"] = ph_mod


class _Logger:
    def __init__(self): self.lines = []
    def info(self, m, *a): self.lines.append(("INFO", str(m) % a if a else str(m)))
    def warning(self, m, *a): self.lines.append(("WARN", str(m) % a if a else str(m)))
    def error(self, m, *a): self.lines.append(("ERROR", str(m) % a if a else str(m)))
    def debug(self, m, *a): self.lines.append(("DEBUG", str(m) % a if a else str(m)))


class BasePlugin:
    def __init__(self, ctx, cfg): self.ctx, self.cfg = ctx, cfg


class Priority:
    HIGH = 50


def _deco(*a, **k):
    def wrap(fn): return fn
    return wrap


on = types.SimpleNamespace(after_xml_parse=lambda priority=None: _deco())
plugin_mod.BasePlugin = BasePlugin
plugin_mod.Priority = Priority
plugin_mod.on = on
plugin_mod.logger = _Logger()


class Text:
    def __init__(self, text): self.text = text


class MessageChain:
    def __init__(self, message_list=None): self.message_list = message_list or []


class KiraMessageBatchEvent: ...


class KiraIMSentResult:
    def __init__(self, message_id=None, is_notice=False, ok=True, err=""):
        self.message_id, self.is_notice, self.ok, self.err = message_id, is_notice, ok, err


SENT_BROADCASTS = []


class _EventType:
    ON_MESSAGE_SENT = "on_message_sent"


class _Reg:
    def get_handlers(self, et):
        async def _h(event, chain, result):
            SENT_BROADCASTS.append(
                ("".join(e.text for e in chain.message_list), result.message_id))
        return [types.SimpleNamespace(exec_handler=_h)]


ph_mod.event_handler_reg = _Reg()
ph_mod.EventType = _EventType

me_mod.Text = Text
chat_mod.MessageChain = MessageChain
mu_mod.KiraMessageBatchEvent = KiraMessageBatchEvent
mu_mod.KiraIMSentResult = KiraIMSentResult

spec = importlib.util.spec_from_file_location("foldfan_main", ROOT / "main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
Plugin = mod.ThreePartFormatPlugin

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Client:
    """记录 send_action；forward_mode 控制转发的行为。"""

    def __init__(self, forward_mode="ok"):
        self.calls = []
        self.timeouts = []
        self.forward_mode = forward_mode      # ok / timeout / conn

    async def send_action(self, action, params, timeout=10.0):
        self.calls.append((action, params))
        self.timeouts.append(timeout)
        if action == "send_forward_msg":
            if self.forward_mode == "timeout":
                raise asyncio.TimeoutError("请求 send_forward_msg 超时")
            if self.forward_mode == "conn":
                raise ConnectionError("NapCat WebSocket 未连接")
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1000 + len(self.calls)}}


class LegacyClient:
    """老核心：send_action 没有 timeout 形参。"""

    def __init__(self):
        self.calls = []

    async def send_action(self, action, params):
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 2000 + len(self.calls)}}


class Adapter:
    def __init__(self, client):
        self.name = "qq"
        self.info = types.SimpleNamespace(name="甜斋", self_id="801381308")
        self._c = client

    def get_client(self): return self._c


class Session:
    session_id = "1083150522"
    sid = "qq:gm:1083150522"


def make_event(client, stopped=False):
    ev = KiraMessageBatchEvent()
    ev.adapter = Adapter(client)
    ev.session = Session()
    ev.self_id = "801381308"
    ev.sid = "qq:gm:1083150522"
    ev.is_stopped = stopped
    ev.is_group_message = lambda: True
    return ev


def make_ctx(client):
    adapter = Adapter(client)
    return types.SimpleNamespace(
        adapter_mgr=types.SimpleNamespace(get_adapter=lambda name: adapter))


SEG_TEXTS = ("官人放心，奴正常得很，三叠这就为您摆开",
             "[3p]\n📚 百科秘书：名单更新\n[/3p]")


def make_chains():
    return [MessageChain([Text(SEG_TEXTS[0]), Text(SEG_TEXTS[1])])]


def run(coro):
    return asyncio.run(coro)


print("═══ 1) 正常：穗 + 转发各一次，转发带 60s")
client = Client()
plugin = Plugin(make_ctx(client), {"enabled": True, "only_group": True})
run(plugin.on_after_xml_parse(make_event(client), make_chains()))
kinds = [a for a, _ in client.calls]
check("穗 + 转发各发一次", kinds == ["send_group_msg", "send_forward_msg"], str(kinds))
check("★ 转发带上了 60s（不是框架默认 10s）", client.timeouts[1] == 60.0,
      str(client.timeouts))

print()
print("═══ 2) ★ 转发超时 ⇒ 降级（宁可不吞消息）")
client_b = Client(forward_mode="timeout")
plugin_b = Plugin(make_ctx(client_b), {"enabled": True, "only_group": True})
chains_b = make_chains()
run(plugin_b.on_after_xml_parse(make_event(client_b), chains_b))
check("★ 超时后产生降级链（不吞消息）", len(chains_b) >= 1, f"剩余链 {len(chains_b)}")
check("降级链里含卡片正文（未发部分）",
      bool(chains_b) and any("百科秘书" in "".join(e.text for e in c.message_list)
                             for c in chains_b),
      str([("".join(e.text for e in c.message_list))[:30] for c in chains_b]))
check("降级链里不含已发出的穗文本",
      bool(chains_b) and SEG_TEXTS[0] not in "".join(e.text for e in chains_b[0].message_list),
      "穗被重复降级")

print()
print("═══ 3) 明确故障（连接断开）⇒ 同样降级")
client_c = Client(forward_mode="conn")
plugin_c = Plugin(make_ctx(client_c), {"enabled": True, "only_group": True})
chains_c = make_chains()
run(plugin_c.on_after_xml_parse(make_event(client_c), chains_c))
check("产生降级链", len(chains_c) >= 1, f"剩余链 {len(chains_c)}")

print()
print("═══ 4) forward_timeout 可配置")
client_d = Client()
plugin_d = Plugin(make_ctx(client_d), {"enabled": True, "only_group": True,
                                       "forward_timeout": 25})
run(plugin_d.on_after_xml_parse(make_event(client_d), make_chains()))
check("传入 25s", client_d.timeouts[1] == 25.0, str(client_d.timeouts))

client_e = Client()
plugin_e = Plugin(make_ctx(client_e), {"enabled": True, "only_group": True,
                                       "forward_timeout": 0})
run(plugin_e.on_after_xml_parse(make_event(client_e), make_chains()))
check("0 ⇒ 用框架默认 10s", client_e.timeouts[1] == 10.0, str(client_e.timeouts))

print()
print("═══ 5) 老核心（无 timeout 形参）自动退回")
client_f = LegacyClient()
plugin_f = Plugin(make_ctx(client_f), {"enabled": True, "only_group": True})
run(plugin_f.on_after_xml_parse(make_event(client_f), make_chains()))
check("老核心下仍能正常发出 2 条",
      [a for a, _ in client_f.calls] == ["send_group_msg", "send_forward_msg"],
      str([a for a, _ in client_f.calls]))

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("✗ " + "; ".join(FAIL))
    sys.exit(1)
print("🎉 全部通过 —— 超时可配置，超时降级不吞消息")
