"""★★★ 折扇 v1.0.2 守卫测试：同一事件重复派发不得重复发送（2026-10-05）。

## 背景（线上事故）

抢先发送插件（accelerator）在流式期间会**补广播 AFTER_XML_PARSE**，
框架最终发送时又会派发一次 —— 同一个 event 对象、同一份链，来两遍。
v1.0.1 没有幂等 ⇒ 转换发送执行两遍 ⇒ 穗文本 ×2 + 合并转发 ×2。

v1.0.2 起：指纹记在 event 上，第二遍静默吃掉；异常只对未发部分降级；
补广播 ON_MESSAGE_SENT；is_stopped / sid 解析 / self_id 一并修好。

## 运行

    python3 tests/test_no_duplicate.py

自包含：桩掉 core.* 依赖，跑的是**仓库里真实的 main.py**。
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


SENT_BROADCASTS = []   # ON_MESSAGE_SENT 补广播记录


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

# ── 加载真实插件代码 ──────────────────────────────────────────
spec = importlib.util.spec_from_file_location("foldfan_main", ROOT / "main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
Plugin = mod.ThreePartFormatPlugin

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Client:
    """记录所有 send_action；可配置在第 N 次调用抛错。"""

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    async def send_action(self, action, params):
        self.calls.append((action, params))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("模拟网络失败")
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1000 + len(self.calls)}}


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


print("═══ 1) ★★★ 同一 event 派发两遍 ⇒ 只发一轮（修复核心）")
client = Client()
plugin = Plugin(make_ctx(client), {"enabled": True, "only_group": True})
ev = make_event(client)
chains1 = make_chains()
run(plugin.on_after_xml_parse(ev, chains1))
n_after_first = len(client.calls)
chains2 = make_chains()          # 框架那遍：同样的内容再来一次
run(plugin.on_after_xml_parse(ev, chains2))
kinds = [a for a, _ in client.calls]
check("第一遍发出 穗+转发 共 2 条", n_after_first == 2, str(kinds))
check("★★★ 第二遍 0 条（幂等生效，修前会再发 2 条）",
      len(client.calls) == 2, f"共 {len(client.calls)} 条")
check("两遍之后链都被移除（框架不会再发原文）",
      chains1 == [] and chains2 == [])
check("转发节点的 uin 是真实 self_id（不再是 0）",
      client.calls[1][1]["messages"][0]["data"]["uin"] == "801381308")
check("目标群号来自 sid 解析", client.calls[0][1]["group_id"] == 1083150522)
check("★ 补广播了 ON_MESSAGE_SENT（sustained/memory 能看到）",
      len(SENT_BROADCASTS) == 2 and all(mid for _, mid in SENT_BROADCASTS),
      str(SENT_BROADCASTS))

print()
print("═══ 2) 不同 event（新的一轮）同样内容 ⇒ 必须照常发送")
SENT_BROADCASTS.clear()
ev2 = make_event(client)
chains3 = make_chains()
run(plugin.on_after_xml_parse(ev2, chains3))
check("新一轮不受上一轮指纹影响，又发了 2 条", len(client.calls) == 4,
      f"累计 {len(client.calls)} 条")

print()
print("═══ 3) ★★ 异常路径：转发失败 ⇒ 只降级未发部分（穗不重复）")
client_b = Client(fail_at=2)     # 第 2 次调用（转发）抛错
plugin_b = Plugin(make_ctx(client_b), {"enabled": True, "only_group": True})
ev_b = make_event(client_b)
chains_b = make_chains()
run(plugin_b.on_after_xml_parse(ev_b, chains_b))
check("穗已发出（第 1 次调用成功）", len(client_b.calls) == 2)
check("降级链里**不含**穗文本（已发不回退）",
      chains_b and SEG_TEXTS[0] not in chains_b[0].message_list[0].text,
      chains_b[0].message_list[0].text if chains_b else "无降级链")
check("降级链里**含**未发的卡片内容",
      chains_b and "百科秘书" in chains_b[0].message_list[0].text)

print()
print("═══ 4) is_stopped / 普通消息 / 私聊限制")
client_c = Client()
plugin_c = Plugin(make_ctx(client_c), {"enabled": True, "only_group": True})
ev_stop = make_event(client_c, stopped=True)
chains_s = make_chains()
run(plugin_c.on_after_xml_parse(ev_stop, chains_s))
check("is_stopped ⇒ 不发、链原样保留", len(client_c.calls) == 0 and len(chains_s) == 1)
plain = [MessageChain([Text("没有标签的普通消息")])]
run(plugin_c.on_after_xml_parse(make_event(client_c), plain))
check("普通消息原样保留、不发送", len(client_c.calls) == 0 and len(plain) == 1)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 重复发送已根治，异常降级不再重复已发部分")
