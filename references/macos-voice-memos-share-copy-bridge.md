# macOS Voice Memos share-copy bridge

Use this when a user gives only a Voice Memos screenshot/Appshot and asks for transcription. This captures the durable workflow learned from a session where direct container access was blocked but Voice Memos could export a temporary audio file via the share sheet.

## Why this exists

Voice Memos recordings live under private Apple containers such as:

```text
~/Library/Containers/com.apple.VoiceMemos
~/Library/Group Containers/group.com.apple.VoiceMemos.shared
```

Terminal access may see the path but still fail with macOS privacy/TCC:

```text
Operation not permitted
```

`lsof -p <VoiceMemosPID>` may reveal an open file like:

```text
.../group.com.apple.VoiceMemos.shared/Recordings/ApplicationAssets/<UUID>/composedAsset.m4a
```

That proves the selected recording but does not guarantee the agent can `cp` it. The reliable bridge is to let Voice Memos itself export a temporary item-provider file.

## Workflow

1. Identify selected recording from screenshot/Appshot/AX tree.
   - Record title, displayed date/time, and duration.
   - Example AX labels: `New Recording 3, 12:31`, duration `36:52`.

2. Try read-only discovery first.
   - Check whether the user already supplied `# Files mentioned by the user` with a `.m4a` path.
   - Check stable exported locations if obvious.
   - If searching private containers returns `Operation not permitted`, do not claim the route is impossible.

3. Use Voice Memos UI export.
   - Select the recording in Voice Memos.
   - Click toolbar `Share` / `共享`.
   - In the share sheet, click `Copy` / `拷贝`.
   - This should put several clipboard flavors on the pasteboard, including `«class M4A »` and `«class furl»`.

4. Inspect the clipboard from terminal:

```bash
osascript -e 'clipboard info'
pbpaste | LC_ALL=C sed -n l | sed -n '1,20p'
osascript -e 'try' \
  -e 'the clipboard as «class furl»' \
  -e 'on error errMsg number errNum' \
  -e '"ERR " & errNum & ": " & errMsg' \
  -e 'end try'
```

Expected `pbpaste` shape:

```text
/Users/<user>/Library/Containers/com.apple.VoiceMemos/Data/tmp/.com.apple.uikit.itemprovider.temporary.<id>/<Title>.m4a
```

5. Copy immediately to a stable path.

The itemprovider file is temporary. Copy it right away into the project, with a clean ASCII/no-space filename:

```bash
mkdir -p transcription_work/work_clean/<slug>/source
cp '/Users/.../.com.apple.uikit.itemprovider.temporary.<id>/<Title>.m4a' \
  transcription_work/work_clean/<slug>/source/<slug>_original.m4a
ffprobe -v error -show_entries format=duration,size,format_name -of default=nw=1 \
  transcription_work/work_clean/<slug>/source/<slug>_original.m4a
```

Verify duration against the Voice Memos UI before continuing.

6. Continue normal AGY workflow.
   - Generate `upload.wav` or `upload.m4a` for direct AGY audio attachment.
   - Run a 30–45s AGY probe.
   - Only run the full transcript after the probe matches the source topic.

## CUA / background-control pitfalls

- A Voice Memos window may be readable by AX/CUA while off-Space or not actually on screen. In that state the toolbar `Share` button can appear in the AX tree, but clicking it may not open the share sheet.
- Do not repeatedly report success from AX clicks unless the post-action snapshot shows the share sheet (`ShareSheet.Popover`) and a `Copy`/`拷贝` button.
- If the window is off-Space/minimized and share sheet will not appear, ask before foregrounding/raising Voice Memos. Alternative: ask the user to manually click `Share` → `Copy`; then resume from clipboard inspection.
- `Cmd+C` or the app menu `Edit → Copy` is not always equivalent to share-sheet `Copy`; verify by checking clipboard classes for `«class M4A »` / `«class furl»`.

## Evidence of success

A successful bridge should show:

```text
«class utf8», ..., «class M4A », <bytes>, «class furl», ..., string, ..., Unicode text, ...
```

and `pbpaste` should print a temporary `.m4a` path. The subsequent stable copy plus `ffprobe` duration check is the proof that the audio source is real.
