#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代码识别副驾 · v1.7
框选一段代码 → 浮层讲解 → 可「🔧 纠错」、底部「追问」深挖、点外面/ESC 自动消失。
代码块可「⭐ 收藏」进「📚 代码库」，库里点一行即复制、可直接粘进终端。
所有动作与异常写入 copilot.log（架构见 ARCHITECTURE.md）。
"""

import base64
import datetime
import html
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

import markdown
import webview

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SNIPPETS_PATH = os.path.join(HERE, "snippets.json")
LOG_PATH = os.path.join(HERE, "copilot.log")

# ── 排错钩子：所有生命周期 + 异常都写进 copilot.log，出 bug 一看就知在哪 ──
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
log = logging.getLogger("copilot")


def _excepthook(t, v, tb):          # 主线程没接住的异常
    log.error("未捕获异常", exc_info=(t, v, tb))


sys.excepthook = _excepthook
if hasattr(threading, "excepthook"):   # 子线程（流式/追问都在子线程）没接住的异常
    threading.excepthook = lambda a: log.error(
        "线程异常", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def logged(fn):
    """给每个界面操作埋点：进来记一行、报错记完整堆栈并保持工具存活。
    这就是「架构里的钩子」——任何按钮/动作出错，copilot.log 里都能直接定位到函数名+堆栈。"""
    def wrapper(self, *a):
        log.info("→ %s%s", fn.__name__, (a if a else ""))
        try:
            return fn(self, *a)
        except Exception:
            log.exception("✗ %s 出错，参数=%r", fn.__name__, a)
            return None
    wrapper.__name__ = fn.__name__
    return wrapper

PROMPT_EXPLAIN = """你是帮我学 coding 的助手。图里是一段代码或命令。我是初学者，目标是**快速看懂它在干嘟**，不是读长篇。

怎么讲：
1. **先一句话总览**：这整段在干嘟。
2. 再**只挑关键的地方**讲——一处一小段，说清"是什么 + 为什么这么写"。显而易见的略过，别逐字逐符号都讲。
3. **简洁、连贯**，像朋友三两句讲明白。别啰嗦、别拆成十小块重复说、**不要写"总结"段**。
4. 术语用大白话解释一句就够；**打比方只在真能帮理解时才用，别硬凑**。
5. 不确定的直说"这点我不确定"，绝不编。

这是"讲解"模式，**只讲它干嘟，不用检查对错。** 如果图里是 Git diff（带 -/+），以 + 那版为准。全程中文、平实说人话，让我一遍看懂。"""

FIX_MSG = "请专门帮我 debug 图里这段代码：一句话说清错在哪、给出改对后的完整代码（放代码块）、简短说为什么。只聚焦纠错，别长篇讲解。如果其实没错，就说'这段看着没问题'。中文、说人话。"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_snippets():
    try:
        with open(SNIPPETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_snippets(snips):
    with open(SNIPPETS_PATH, "w", encoding="utf-8") as f:
        json.dump(snips, f, ensure_ascii=False, indent=2)


_CJK = re.compile(r"[一-鿿]")


def clean_code(code):
    """收进库前清洗：砍掉含中文的注释（# 或 // 起头）和纯中文说明行，
    保留真正的命令（含命令里合法的中文字符串参数，那种删了会废）。"""
    out = []
    for line in code.splitlines():
        # 砍掉含中文的注释段：# 或 // 必须在行首、或前面是空格（这样 http:// 不会被误伤）
        cut = re.sub(r"(?:^|(?<=\s))(#|//).*[一-鿿].*$", "", line).rstrip()
        if cut.strip():
            out.append(cut)
        # 整行被清空（纯中文注释行）→ 丢掉
    cleaned = "\n".join(out).strip()
    return cleaned or code.strip()   # 万一整块都是中文，兜底存原文


def pick_folder():
    """用系统的 osascript 弹文件夹选择框（独立进程，不卡 pywebview）。取消返回空。"""
    try:
        r = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "选一个收藏文件夹")'],
            capture_output=True, text=True, timeout=180)
        return r.stdout.strip()
    except Exception:
        return ""


def capture_region():
    path = os.path.join(tempfile.gettempdir(), "code_copilot_snap.png")
    if os.path.exists(path):
        os.remove(path)
    subprocess.run(["screencapture", "-i", path])
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    return path


def md_to_html(text):
    return markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists", "nl2br"])


def image_message(image_path):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return {"role": "user", "content": [
        {"type": "text", "text": PROMPT_EXPLAIN},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]}


def stream_chat(cfg, messages, on_chunk):
    body = {"model": cfg["model"], "messages": messages, "max_tokens": 4000, "stream": True}
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
    )
    full = []
    with urllib.request.urlopen(req, timeout=90) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0].get("delta", {}).get("content")
            except Exception:
                continue
            if delta:
                full.append(delta)
                on_chunk(delta)
    return "".join(full)


HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
:root{--bg:#1e1e2e;--panel:#181825;--fg:#e4e4ef;--sub:#9399b2;--accent:#89b4fa;--green:#a6e3a1;--fix:#fab387;--code:#313244;--border:#313244}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,"PingFang SC",sans-serif;font-size:15px;line-height:1.75}
#bar{position:sticky;top:0;display:flex;flex-wrap:wrap;gap:6px;padding:10px 14px;background:rgba(24,24,37,.92);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--border);z-index:10}
#bar button{background:var(--code);color:var(--fg);border:none;border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer;transition:.15s}
#bar button:hover{background:var(--accent);color:var(--bg)}
#bar button.fix{background:rgba(250,179,135,.16);color:var(--fix)}
#bar button.fix:hover{background:var(--fix);color:var(--bg)}
#content{padding:18px 24px 24px;-webkit-user-select:text;user-select:text;cursor:auto}
#bar,#askbar,.savebtn,#selsave{-webkit-user-select:none;user-select:none}
.loading{color:var(--sub);padding:34px 6px;font-size:15px}
.ask{color:var(--fix);font-weight:600;margin:6px 0 2px}
h1{font-size:22px;color:var(--accent);border-bottom:2px solid var(--border);padding-bottom:8px;margin:16px 0 12px}
h2{font-size:18px;color:var(--accent);margin:24px 0 10px;padding-left:11px;border-left:4px solid var(--accent)}
h3{font-size:16px;color:var(--green);margin:18px 0 8px}
strong{color:#fff;font-weight:700}
p{margin:10px 0}
code{background:var(--code);padding:2px 6px;border-radius:5px;font-family:"SF Mono",Menlo,monospace;font-size:13px;color:#f5e0dc}
pre{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:30px 16px 14px;overflow-x:auto;margin:12px 0;position:relative}
pre code{background:none;padding:0;color:#cdd6f4;font-size:13.5px;line-height:1.6}
.savebtn{position:absolute;top:7px;right:9px;background:rgba(137,180,250,.16);color:var(--accent);border:none;border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer}
.savebtn:hover{background:var(--accent);color:var(--bg)}
.snip{display:flex;align-items:center;gap:10px;background:var(--code);border:1px solid var(--border);border-radius:9px;padding:10px 13px;margin:8px 0;cursor:pointer;transition:.12s}
.snip:hover{border-color:var(--accent);background:#3a3a4e}
.snip code{flex:1;background:none;padding:0;color:#cdd6f4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.snip .del{flex:none;color:var(--sub);font-size:13px}
.snip .del:hover{color:#f38ba8}
.back{display:inline-block;margin-bottom:6px;background:var(--code);color:var(--fg);border:none;border-radius:8px;padding:6px 13px;cursor:pointer;font-size:13px}
.back:hover{background:var(--accent);color:var(--bg)}
.empty{color:var(--sub);padding:24px 4px;line-height:2}
blockquote{margin:12px 0;padding:10px 15px;background:rgba(137,180,250,.09);border-left:4px solid var(--accent);border-radius:0 8px 8px 0}
blockquote p{margin:4px 0}
ul,ol{padding-left:24px;margin:10px 0}li{margin:5px 0}
table{border-collapse:collapse;margin:12px 0;width:100%;font-size:14px}
th,td{border:1px solid var(--border);padding:7px 10px;text-align:left}th{background:var(--code)}
hr{border:none;border-top:1px solid var(--border);margin:20px 0}
#askbar{position:sticky;bottom:0;display:flex;gap:8px;padding:10px 14px;background:rgba(24,24,37,.95);-webkit-backdrop-filter:blur(10px);border-top:1px solid var(--border)}
#q{flex:1;background:var(--code);color:var(--fg);border:1px solid var(--border);border-radius:9px;padding:9px 12px;font-size:14px;outline:none}
#q:focus{border-color:var(--accent)}
#send{background:var(--accent);color:var(--bg);border:none;border-radius:9px;padding:0 16px;font-size:14px;cursor:pointer}
#toast{position:fixed;bottom:64px;left:50%;transform:translateX(-50%);background:var(--accent);color:var(--bg);padding:8px 18px;border-radius:20px;font-size:13px;opacity:0;transition:.3s;pointer-events:none;z-index:20}
#toast.show{opacity:1}
#selsave{position:fixed;display:none;z-index:30;background:var(--accent);color:var(--bg);border:none;border-radius:7px;padding:5px 12px;font-size:12px;cursor:pointer;box-shadow:0 3px 12px rgba(0,0,0,.45)}
#selsave:hover{filter:brightness(1.08)}
</style></head><body>
<div id="bar">
<button class="fix" onclick="pywebview.api.fix()">🔧 纠错</button>
<button onclick="pywebview.api.copy().then(()=>toast('已复制'))">📋 复制</button>
<button onclick="busy();pywebview.api.set_folder().then(m=>{toast(m);unbusy();})">📁 文件夹</button>
<button onclick="busy();pywebview.api.collect().then(m=>{toast(m);unbusy();})">⭐ 收藏</button>
<button onclick="pywebview.api.undo_collect().then(m=>toast(m))">↩︎ 撤销</button>
<button onclick="showLibrary()">📚 库</button>
<button onclick="pywebview.api.close()">✕</button>
</div>
<div id="content"><div class="loading">🔍 正在识别 + 讲解，稍等…（模型开口后会一行行冒出来）</div></div>
<div id="askbar">
<input id="q" placeholder="针对哪块没懂？打字，回车追问…" autocomplete="off">
<button id="send" onclick="send()">发送</button>
</div>
<button id="selsave" onmousedown="event.preventDefault()" onclick="saveSel()">⭐ 收藏选中</button>
<div id="toast"></div>
<script>
function render(html,top){var c=document.getElementById('content');c.innerHTML=html;decorate();window.scrollTo(0,top?0:document.body.scrollHeight);}
function send(){var i=document.getElementById('q');var v=i.value;if(!v.trim())return;i.value='';pywebview.api.ask(v);}
function decorate(){document.querySelectorAll('#content pre').forEach(function(pre){if(pre.dataset.d)return;pre.dataset.d='1';var code=pre.innerText;var b=document.createElement('button');b.textContent='⭐ 收藏';b.className='savebtn';b.onclick=function(){pywebview.api.save_snippet(code).then(m=>toast(m));};pre.appendChild(b);});}
var _prev='';
function showLibrary(){var c=document.getElementById('content');if(!c.dataset.lib){_prev=c.innerHTML;}pywebview.api.library_html().then(function(h){c.innerHTML=h;c.dataset.lib='1';window.scrollTo(0,0);});}
function backFromLib(){var c=document.getElementById('content');c.innerHTML=_prev;c.dataset.lib='';decorate();window.scrollTo(0,0);}
var _busy=false,_armed=false;function busy(){_busy=true;}function unbusy(){setTimeout(function(){_busy=false;},500);}
setTimeout(function(){_armed=true;},900);
window.addEventListener('blur',function(){if(_armed&&!_busy&&window.pywebview&&pywebview.api)pywebview.api.close();});
document.addEventListener('keydown',function(e){if(e.key==='Enter'&&document.activeElement.id==='q')send();else if(e.key==='Escape'&&window.pywebview&&pywebview.api)pywebview.api.close();});
var _t;function toast(m){if(!m)return;var e=document.getElementById('toast');e.textContent=m;e.classList.add('show');clearTimeout(_t);_t=setTimeout(function(){e.classList.remove('show')},1600);}
var _seltext='';
function hideSel(){var b=document.getElementById('selsave');if(b)b.style.display='none';}
function saveSel(){if(_seltext&&window.pywebview&&pywebview.api)pywebview.api.save_snippet(_seltext).then(m=>toast(m));hideSel();var s=window.getSelection();if(s)s.removeAllRanges();}
document.addEventListener('mouseup',function(e){
  if(e.target&&e.target.id==='selsave')return;
  var sel=window.getSelection();var txt=((sel&&sel.toString())||'').trim();var c=document.getElementById('content');
  if(!txt||!sel.anchorNode||!c.contains(sel.anchorNode)){hideSel();return;}
  _seltext=txt;var r=sel.getRangeAt(0).getBoundingClientRect();var b=document.getElementById('selsave');
  b.style.display='block';var t=r.top-36;if(t<44)t=r.bottom+8;
  b.style.top=t+'px';b.style.left=Math.max(8,Math.min(r.left,window.innerWidth-130))+'px';
});
document.addEventListener('scroll',hideSel,true);
</script></body></html>"""


def run_turn(api):
    api.busy = True
    window = api.window
    base = api.rendered
    buf, last = [], [0.0]

    def on_chunk(piece):
        buf.append(piece)
        now = time.monotonic()
        if now - last[0] > 0.25:
            last[0] = now
            window.evaluate_js(f"render({json.dumps(base + md_to_html(''.join(buf)))},false)")

    log.info("模型请求：第 %d 轮，model=%s", len(api.messages), api.cfg.get("model"))
    try:
        full = stream_chat(api.cfg, api.messages, on_chunk)
        log.info("模型完成：收到 %d 字", len(full))
    except urllib.error.HTTPError as e:
        log.exception("接口 HTTP 错误 %s", e.code)
        full = f"⚠️ **接口报错 {e.code}**"
    except Exception as e:
        log.exception("模型请求失败")
        full = f"⚠️ **出错了**：{e}\n\n> 若是超时：把 config.json 里 model 换成更快的 `Qwen/Qwen3-VL-8B-Instruct`。"
    api.messages.append({"role": "assistant", "content": full})
    api.rendered = base + md_to_html(full)
    api.transcript += full
    window.evaluate_js(f"render({json.dumps(api.rendered)},{json.dumps(base == '')})")
    api.busy = False


class Api:
    def __init__(self, cfg, image_path):
        self.cfg = cfg
        self.messages = [image_message(image_path)]
        self.rendered = ""
        self.transcript = ""
        self.window = None
        self.busy = False

    def _spawn(self, display_chip, user_text):
        if self.busy:
            return
        self.rendered += f'<hr><div class="ask">{display_chip}</div>'
        self.transcript += f"\n\n**{display_chip}**\n\n"
        self.messages.append({"role": "user", "content": user_text})
        self.window.evaluate_js(f"render({json.dumps(self.rendered)},true)")
        threading.Thread(target=lambda: run_turn(self), daemon=True).start()

    @logged
    def fix(self):
        self._spawn("🔧 纠错这段代码", FIX_MSG)

    @logged
    def ask(self, question):
        q = (question or "").strip()
        if not q:
            return
        esc = q.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._spawn(f"🙋 {esc}", q)

    @logged
    def close(self):
        log.info("关闭窗口（os._exit）")
        logging.shutdown()
        os._exit(0)          # 从副线程秒关整个进程（不碰会卡死的 window.destroy）

    @logged
    def copy(self):
        subprocess.run(["pbcopy"], input=self.transcript.encode("utf-8"))

    @logged
    def set_folder(self):
        path = pick_folder()
        if path:
            self.cfg["save_folder"] = path
            save_config(self.cfg)
            return "文件夹已设：" + os.path.basename(path.rstrip("/"))
        return ""

    @logged
    def collect(self):
        if not self.cfg.get("save_folder"):
            if not self.set_folder():
                return "没选文件夹"
        folder = self.cfg["save_folder"]
        os.makedirs(folder, exist_ok=True)
        fp = os.path.join(folder, "代码识别副驾.md")
        block = f"\n\n---\n\n### {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n{self.transcript}\n"
        with open(fp, "a", encoding="utf-8") as f:
            f.write(block)
        self._last_file, self._last_block = fp, block
        return "已收藏 ✓（可撤销）"

    @logged
    def undo_collect(self):
        fp = getattr(self, "_last_file", None)
        blk = getattr(self, "_last_block", None)
        if not fp or not blk or not os.path.exists(fp):
            return "没有可撤销的收藏"
        with open(fp, encoding="utf-8") as f:
            content = f.read()
        if content.endswith(blk):
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content[:-len(blk)])
            self._last_block = None
            return "已撤销收藏 ↩︎"
        return "撤销失败（文件被改过了）"

    # ── 代码库：每个代码块可收藏，库里点一行即复制 ──
    @logged
    def save_snippet(self, code):
        code = clean_code((code or "").strip())
        if not code:
            return ""
        snips = load_snippets()
        snips.append({"code": code, "at": datetime.datetime.now().strftime("%m-%d %H:%M")})
        save_snippets(snips)
        return "已存进代码库 ✓"

    @logged
    def library_html(self):
        snips = load_snippets()
        head = '<button class="back" onclick="backFromLib()">← 返回讲解</button>'
        if not snips:
            return head + '<p class="empty">代码库还是空的。<br>讲解里每个代码块右上角有「⭐ 收藏」，点它就进来了。</p>'
        rows = [head, '<h2>📚 代码库</h2><p style="color:var(--sub);margin:2px 0 12px;font-size:13px">点一行 = 复制整段，可直接粘进终端。</p>']
        for i in range(len(snips) - 1, -1, -1):
            preview = html.escape(snips[i]["code"].replace("\n", " ⏎ "))
            rows.append(
                f'<div class="snip" onclick="pywebview.api.copy_snippet({i}).then(()=>toast(\'已复制 ✓\'))">'
                f'<code>{preview}</code>'
                f'<span class="del" onclick="event.stopPropagation();pywebview.api.del_snippet({i}).then(showLibrary)">🗑</span></div>')
        return "".join(rows)

    @logged
    def copy_snippet(self, i):
        snips = load_snippets()
        if 0 <= i < len(snips):
            subprocess.run(["pbcopy"], input=snips[i]["code"].encode("utf-8"))

    @logged
    def del_snippet(self, i):
        snips = load_snippets()
        if 0 <= i < len(snips):
            snips.pop(i)
            save_snippets(snips)


def _pin_over_fullscreen(window):
    """让浮层能叠在当前画面（含全屏 App / 当前 Space）之上，
    而不是被 macOS 切回桌面空间才显示——修"在全屏网页里框选会跳回桌面"。"""
    def apply():
        try:
            from webview.platforms import cocoa
            ns = cocoa.BrowserView.instances[window.uid].window
            # CanJoinAllSpaces(1<<0) | FullScreenAuxiliary(1<<8)：叠在全屏之上、跟随当前空间
            ns.setCollectionBehavior_((1 << 0) | (1 << 8))
            log.info("已设跨空间行为：可叠在全屏/当前空间之上")
        except Exception:
            log.exception("设置跨空间行为失败（不影响其它功能）")

    try:
        window.events.shown += apply
    except Exception:
        log.exception("订阅 shown 事件失败")


def run_gui(cfg, image_path):
    api = Api(cfg, image_path)
    window = webview.create_window(
        "代码识别副驾", html=HTML, js_api=api,
        width=540, height=600, on_top=True, text_select=True,
        background_color="#1e1e2e",
    )
    api.window = window
    _pin_over_fullscreen(window)
    webview.start(lambda: run_turn(api))


def run_once():
    log.info("=== 唤起 ⌘⇧B ===")
    try:
        cfg = load_config()
    except Exception:
        log.exception("读取 config.json 失败")
        subprocess.run(["osascript", "-e",
                        'display alert "配置读取失败" message "config.json 有问题，详见 copilot.log"'])
        return
    if not cfg.get("api_key") or "粘贴" in cfg["api_key"]:
        log.warning("api_key 未填，退出")
        subprocess.run(["osascript", "-e",
                        'display alert "还没填 key" message "请在 config.json 里填入 api_key"'])
        return
    img = capture_region()
    if img is None:
        log.info("未截图（取消框选），退出")
        return
    log.info("截图完成：%s", img)
    run_gui(cfg, img)


if __name__ == "__main__":
    run_once()
