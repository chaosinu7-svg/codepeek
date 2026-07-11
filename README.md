# 代码识别副驾

> 学 coding 时，看到一段代码或报错，**框一下就有人用大白话讲给你听**——不用切软件、不用复制粘贴到别处问。

一个 Mac 本地小工具。全局热键 `⌘⇧B` 框选屏幕上任意一段代码，浮层里立刻：

- 🗣️ **讲解**：这段在干嘟，挑关键处说人话，不啰嗦
- 🔧 **纠错**：报错了？专门 debug，给出改对后的完整代码
- 🙋 **追问**：没懂的点接着问，多轮对话
- 📚 **代码库**：讲解里的命令一键攒起来，点一行即复制，直接粘进终端

用完点一下外面就消失，不占地方。

---

## 演示

> _（放一张 GIF 或截图在这里）_

---

## 安装

需要 macOS + Python 3。

```bash
# 1. 拉代码
git clone <你的仓库地址>
cd 代码识别副驾

# 2. 建虚拟环境、装依赖
python3 -m venv .venv
.venv/bin/pip install pywebview markdown

# 3. 配置（复制模板，填入你的 API key）
cp config.example.json config.json
#   然后编辑 config.json，把 api_key 换成你自己的
#   模型走硅基流动 / 任意 OpenAI 兼容平台，默认 Qwen3-VL

# 4. 装全局热键（skhd）
brew install skhd
#   在 ~/.config/skhd/skhdrc 里加一行（路径换成你的）：
#   cmd + shift - b : /绝对路径/.venv/bin/python3 /绝对路径/code_copilot.py
skhd --start-service
#   首次需在「系统设置 → 隐私与安全性 → 辅助功能」里给 skhd 打勾
```

装好后，任何软件里按 **⌘⇧B** 即可。

---

## 使用

| 操作 | 怎么做 |
|------|--------|
| 起工具 | 按 `⌘⇧B`，拉框选中一段代码 |
| 追问 | 底部输入框打字，回车 |
| 纠错 | 工具栏 `🔧 纠错` |
| 收藏一段命令 | 代码块右上角 `⭐`，或**划选任意文字**后点冒出的 `⭐ 收藏选中` |
| 打开代码库 | 工具栏 `📚 库`，点某一行即复制 |
| 关闭 | 点窗口外面，或 `ESC` |

---

## 配置（config.json）

| 字段 | 说明 |
|------|------|
| `base_url` | OpenAI 兼容接口地址，默认硅基流动 |
| `api_key` | 你的 key（**不会进仓库**，已被 `.gitignore` 挡住） |
| `model` | 视觉模型。`Qwen/Qwen3-VL-30B-A3B-Instruct` 准但稍慢；`Qwen/Qwen3-VL-8B-Instruct` 更快 |
| `save_folder` | 「⭐ 收藏对话」存哪，留空则首次收藏时弹窗让你选 |

---

## 架构 & 排错

- 功能全景、数据流、设计决策见 [ARCHITECTURE.md](ARCHITECTURE.md)。
- 所有动作和异常都写进 `copilot.log`，出 bug 时看它——每条错误都带函数名和精确行号的堆栈。

---

## 说明

个人学习用小工具，只做 Mac，不做跨平台。讲解质量取决于所用视觉模型。
