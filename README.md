# agy-cli-transcribe

用 Gemini 把长录音转成带说话人的文字稿。一个给 AI Agent（Claude Code、Codex 等）用的 skill。

它走的是 Antigravity 的命令行 `agy`，用的是 Gemini 会员额度，不用另外买 API。

## 它做了什么

1. 把录音切成 5 分钟一段（单声道 16kHz，前后重叠 20 秒），并行转写，最后拼回一份文字稿。
2. 没有人声的片段直接标 `[无人声]`，不发给模型，免得模型编内容。
3. 检查模型有没有真的打开音频文件。没打开就判失败重试，不收它编出来的答案。
4. 失败或可疑的片段，可以用 60 秒的小窗口补转。
5. 输出一份文字稿和一份待核对清单，转错的、没转出来的地方都标出来。

## 需要先装好

1. Antigravity 命令行 `agy`，并且已经登录。
2. `ffmpeg` 和 `ffprobe`（Mac 上：`brew install ffmpeg`）。
3. Python 3。主流程只用标准库。

我是在 Mac 上用的，其他系统没测过。

## 怎么装

把整个 `agy-cli-transcribe` 文件夹放进你的 Agent 的 skills 目录，比如 Claude Code 是 `~/.claude/skills/`，Codex 是 `~/.codex/skills/`。

## 怎么用

直接跟你的 Agent 说「用 agy-cli-transcribe 把这个录音转成文字稿」，再把录音路径给它。

它会先跑一小段试转，告诉你还剩多少额度，确认没问题再跑完整的。详细流程在 `SKILL.md` 和 `references/workflow.md`。

## 备用路线

`scripts/fallback.py` 里还有 ElevenLabs、Gemini API 两条备用路线，要用你自己的 API key。不用也不影响主流程。什么时候该换路线，见 `references/routing.md`。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 反馈

用的时候卡住了，或者转出来有问题，欢迎提 Issue。最好带上：录音多长、什么语言；卡在哪一步、报了什么错；用的是哪个 Agent。

## License

MIT
