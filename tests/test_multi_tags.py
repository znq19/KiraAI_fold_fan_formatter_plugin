"""★★★ 折扇 v1.1.0 守卫测试：多等价标签（list 配置）与老配置兼容（2026-10-21）。

## 背景

v1.1.0 起 `tag_start` / `tag_end` 由 string 改为 **list**（WebUI 一回车一个）：
- 任一起始标签与任一结束标签均视为合法配对（交叉配对 `[3p]…[/p]` 合法）；
- 解决能力较弱的 LLM 固定输错某个标签值导致不触发的问题；
- 老的单字符串配置运行时自动归一，且升级后一次性迁移为列表
  （config_migrations.py，走框架 update_plugin_config 通道）。

## 运行

    python3 tests/test_multi_tags.py

自包含：桩掉 core.* 依赖，跑的是**仓库里真实的 main.py 与 config_migrations.py**。
"""
import asyncio
import importlib.util
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── 桩 core.*（与 tests/test_no_duplicate.py 同一套） ─────────────
core = types.ModuleType("core"); sys.modules["core"] = core
plugin_mod = types.ModuleType("core.plugin"); sys.modules["core.plugin"] = plugin_mod
chat_mod = types.ModuleType("core.chat"); sys.modules["core.chat"] = chat_mod
mu_mod = types.ModuleType("core.chat.message_utils"); sys.modules["core.chat.message_utils"] = mu_mod
me_mod = types.ModuleType("core.chat.message_elements"); sys.modules["core.chat.message_elements"] = me_mod


class _Logger:
    def __init__(self): self.lines = []
    def info(self, m, *a): self.lines.append(("INFO", str(m) % a if a else str(m)))
    def warning(self, m, *a): self.lines.append(("WARN", str(m) % a if a else str(m)))
    def error(self, m, *a): self.lines.append(("ERROR", str(m) % a if a else str(m)))
    def debug(self, m, *a): self.lines.append(("DEBUG", str(m) % a if a else str(m)))
    def exception(self, m, *a): self.lines.append(("EXC", str(m) % a if a else str(m)))


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


me_mod.Text = Text
chat_mod.MessageChain = MessageChain
mu_mod.KiraMessageBatchEvent = KiraMessageBatchEvent
mu_mod.KiraIMSentResult = KiraIMSentResult

# ── 加载真实插件代码 ──────────────────────────────────────────
spec = importlib.util.spec_from_file_location("foldfan_main", ROOT / "main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
Plugin = mod.ThreePartFormatPlugin
_normalize = mod._normalize_tag_list
_build = mod._build_tag_pattern

# 迁移模块无任何 core.* 依赖，直接按文件加载
spec_m = importlib.util.spec_from_file_location("foldfan_migrations", ROOT / "config_migrations.py")
mig = importlib.util.module_from_spec(spec_m)
spec_m.loader.exec_module(mig)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Client:
    def __init__(self):
        self.calls = []

    async def send_action(self, action, params):
        self.calls.append((action, params))
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


def make_event(client):
    ev = KiraMessageBatchEvent()
    ev.adapter = Adapter(client)
    ev.session = Session()
    ev.self_id = "801381308"
    ev.sid = "qq:gm:1083150522"
    ev.is_stopped = False
    ev.is_group_message = lambda: True
    return ev


def make_ctx(client):
    adapter = Adapter(client)
    return types.SimpleNamespace(
        adapter_mgr=types.SimpleNamespace(get_adapter=lambda name: adapter))


def run(coro):
    return asyncio.run(coro)


def convert_text(plugin, client, text):
    """跑一遍 on_after_xml_parse，返回 (转发卡片内容列表, 剩余链)。"""
    ev = make_event(client)
    chains = [MessageChain([Text(text)])]
    run(plugin.on_after_xml_parse(ev, chains))
    forwards = [p for a, p in client.calls if a == "send_forward_msg"]
    cards = [f["messages"][0]["data"]["content"][0]["data"]["text"] for f in forwards]
    return cards, chains


print("═══ 1) _normalize_tag_list 归一化")
check("None（键缺失）→ 默认", _normalize(None, ["[3p]"]) == ["[3p]"])
check("str（老配置）→ 单元素列表", _normalize("[dw]", ["[3p]"]) == ["[dw]"])
check("list 原样通过", _normalize(["[a]", "[b]"], ["[3p]"]) == ["[a]", "[b]"])
check("空白剔除 + 保序去重",
      _normalize(["[3p]", " [3p] ", "", "  ", "[dw]"], ["x"]) == ["[3p]", "[dw]"])
check("怪类型（int）→ 默认", _normalize(123, ["[3p]"]) == ["[3p]"])
check("★ 显式空列表 → []（关闭标签触发，不回退默认）", _normalize([], ["[3p]"]) == [])

print()
print("═══ 2) _build_tag_pattern 正则语义")
check("空列表 ⇒ None（关闭标签触发）",
      _build([], ["[/3p]"]) is None and _build(["[3p]"], []) is None)
m = _build(["ab", "abc"], ["z"]).search("abcXz")
check("长标签优先（abc 先于 ab 匹配）", m and m.group(1) == "X",
      m.group(1) if m else "无匹配")
m = _build(["[3p]"], ["[/3p]", "[/p]"]).search("前文[3p]内容[/p]后文")
check("★ 交叉配对 [3p]…[/p] 合法（LLM 写错结束标签也触发）",
      m and m.group(1) == "内容")
m = _build(["[3p]"], ["[/3p]", "[/p]"]).search("[3p]A[/p]B[/3p]")
check("★ 贪婪取最后结束标签（内容里提到 [/p] 不会提前截断）",
      m and m.group(1) == "A[/p]B", m.group(1) if m else "无匹配")
m = _build(["[3p]"], ["[/3p]", "[/p]"]).search(
    "[3p]\n我支持 [3p]…[/3p] 与 [3p]…[/p] 两种写法\n[/3p]")
check("★ 折叠内容里自我解释标签格式（成对提及）安全",
      m and "两种写法" in m.group(1), m.group(1) if m else "无匹配")
m = _build(["[3p]"], ["[/3p]", "[/p]"]).search("[3p]内容[/3p] 尾巴提到[/p]而已")
check("已知取舍：尾巴里的结束标签字样并入卡片（文字不丢，仅位置变化）",
      m and m.group(1) == "内容[/3p] 尾巴提到", m.group(1) if m else "无匹配")

print()
print("═══ 3) 端到端：多标签 / 老配置 / 关闭触发")
client = Client()
plugin = Plugin(make_ctx(client), {
    "enabled": True, "only_group": True,
    "tag_start": ["[3p]", "[dw]"], "tag_end": ["[/3p]", "[/p]", "[/dw]"],
})
cards, chains = convert_text(plugin, client, "[dw]\n多起始标签内容\n[/dw]")
check("多起始标签 [dw]…[/dw] 触发转换", cards == ["多起始标签内容"], str(cards))
check("转换后原链被移除", chains == [])

client2 = Client()
plugin2 = Plugin(make_ctx(client2), {
    "enabled": True, "only_group": True,
    "tag_start": ["[3p]"], "tag_end": ["[/3p]", "[/p]"],
})
cards2, _ = convert_text(plugin2, client2, "[3p]\n交叉配对内容\n[/p]")
check("★ 端到端交叉配对 [3p]…[/p] 触发", cards2 == ["交叉配对内容"], str(cards2))

client3 = Client()
plugin3 = Plugin(make_ctx(client3), {
    "enabled": True, "only_group": True,
    "tag_start": "[3p]", "tag_end": "[/3p]",     # ← 老配置形态（字符串）
})
cards3, _ = convert_text(plugin3, client3, "[3p]\n老配置内容\n[/3p]")
check("★ 老字符串配置运行时自动兼容", cards3 == ["老配置内容"], str(cards3))

client4 = Client()
plugin4 = Plugin(make_ctx(client4), {
    "enabled": True, "only_group": True,
    "tag_start": [], "tag_end": [],
})
cards4, chains4 = convert_text(plugin4, client4, "[3p]\n不应触发\n[/3p]")
check("空列表 ⇒ 标签不触发、链原样保留", cards4 == [] and len(chains4) == 1)

client5 = Client()
plugin5 = Plugin(make_ctx(client5), {
    "enabled": True, "only_group": True,
    "tag_start": [], "tag_end": [],
    "enable_auto_by_length": True, "auto_length_threshold": 10,
})
cards5, _ = convert_text(plugin5, client5, "这是一条没有任何标签但足够长的消息文本")
check("空列表下长度触发仍工作", len(cards5) == 1, str(cards5))

client7 = Client()
plugin7 = Plugin(make_ctx(client7), {
    "enabled": True, "only_group": True,
    "tag_start": ["[3p]", "[dw]"], "tag_end": ["[/3p]", "[/p]", "[/dw]"],
})
cards6, chains6 = convert_text(plugin7, client7, "没有标签的普通消息")
check("多标签配置下普通消息不受影响", cards6 == [] and len(chains6) == 1)

print()
print("═══ 4) 一次性迁移（config_migrations）")
check("decide_plan：字符串键列入计划",
      mig.decide_plan({"tag_start": "[3p]", "tag_end": ["[/3p]"]}) == ["tag_start"])
check("decide_plan：列表/缺失不列入",
      mig.decide_plan({"tag_start": ["[3p]"]}) == [] and mig.decide_plan({}) == [])


class FakeMgr:
    """最小 plugin_mgr：内存配置 + 记录 update 调用。"""

    def __init__(self, cfg, insts):
        self._cfg = cfg
        self.plugin_instances = insts
        self.updates = []

    def is_plugin_enabled(self, pid): return True
    def get_plugin_config(self, pid): return dict(self._cfg)

    async def update_plugin_config(self, pid, patch):
        self.updates.append(dict(patch))
        self._cfg.update(patch)
        return dict(self._cfg)


with tempfile.TemporaryDirectory() as tmp:
    fake_plugin = types.SimpleNamespace(ctx=types.SimpleNamespace())
    mgr = FakeMgr({"tag_start": "[3p]", "tag_end": "[/3p]"}, {"fold_fan_formatter": fake_plugin})
    fake_plugin.ctx.plugin_mgr = mgr
    r = run(mig.run_tags_migration(fake_plugin, "fold_fan_formatter", tmp, _Logger()))
    check("老字符串配置 ⇒ migrated，且值为单元素列表",
          r == "migrated" and mgr.updates == [
              {"tag_start": ["[3p]"], "tag_end": ["[/3p]"]}], f"r={r} updates={mgr.updates}")
    check("迁移标记已写", mig.marker_applied(tmp))
    r2 = run(mig.run_tags_migration(fake_plugin, "fold_fan_formatter", tmp, _Logger()))
    check("第二次运行 ⇒ skip（仅一次）", r2 == "skip" and len(mgr.updates) == 1)

with tempfile.TemporaryDirectory() as tmp2:
    fake_plugin2 = types.SimpleNamespace(ctx=types.SimpleNamespace())
    mgr2 = FakeMgr({"tag_start": ["[3p]"]}, {"fold_fan_formatter": fake_plugin2})
    fake_plugin2.ctx.plugin_mgr = mgr2
    r3 = run(mig.run_tags_migration(fake_plugin2, "fold_fan_formatter", tmp2, _Logger()))
    check("已是列表 ⇒ marked，不调用 update（不触发重载）",
          r3 == "marked" and mgr2.updates == [] and mig.marker_applied(tmp2))

with tempfile.TemporaryDirectory() as tmp3:
    fake_plugin3 = types.SimpleNamespace(ctx=types.SimpleNamespace())
    mgr3 = FakeMgr({"tag_start": " [3p] ", "tag_end": "  "},
                   {"fold_fan_formatter": fake_plugin3})
    fake_plugin3.ctx.plugin_mgr = mgr3
    r4 = run(mig.run_tags_migration(fake_plugin3, "fold_fan_formatter", tmp3, _Logger()))
    check("迁移时顺手 strip（' [3p] ' → ['[3p]']）",
          r4 == "migrated" and mgr3.updates[0]["tag_start"] == ["[3p]"],
          str(mgr3.updates))
    check("全空白病理值回退默认（'  ' → ['[/3p]']）",
          mgr3.updates[0]["tag_end"] == ["[/3p]"], str(mgr3.updates))

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 多等价标签生效，老配置兼容，迁移仅一次且走框架通道")
