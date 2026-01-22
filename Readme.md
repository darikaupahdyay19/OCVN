# Quick Reference Card

## 📥 Download Commands

| Command | Description | Example |
|---------|-------------|---------|
| `/dl <id>` | Download all episodes | `/dl 274` |
| `/dl <id> <ep>` | Download single episode | `/dl 274 5` |
| `/dl <id> <start>-<end>` | Download range | `/dl 274 1-5` |
| `/engdl <id>` | Same as /dl | `/engdl 274` |

## 🎭 Dual Audio Commands

| Command | Description |
|---------|-------------|
| `/dual <id1> <id2>` | Sub video + Dub audio |
| `/engvdiddual <id1> <id2>` | Dub video + Sub audio |

## 🎮 Queue Commands

| Command | Description |
|---------|-------------|
| `/queue` | Show queue status |
| `/cancel` | Cancel current task |

## 📝 Filename Format

**OLD:** `Premium_Guilty_Hole_Room_of_Guilty_Pleasure_Episode_3_Congratulations.mp4`

**NEW:** `Guilty_Hole_E03_[Dub].mp4`

## 📊 Progress Display

```
🎬 Downloading & Uploading [Dub]
📊 Overall: 3/7 (43%)

📁 Current Item Progress:
[████████░░] 80.5%

📦 Size: 450.23 MB / 560.00 MB
⚡ Speed: 12.45 MB/s
⏱️ ETA: 8s
⏳ Total Time: 2m 15s
```

Then:

```
📤 Uploading 3/7
📁 MyAnime_E03_[Dub].mp4
📦 Size: 560.00 MB
```

## ✨ Key Features

✅ **One progress message** for all episodes
✅ **Short filenames** under Telegram limit
✅ **Episode selection** (single or range)
✅ **Queue management** (one task per user)
✅ **Cancel support** (stop anytime)
✅ **Auto cleanup** (files deleted after upload)
✅ **Clean names** (no "Premium" tags)

## 🎯 Common Use Cases

### Download one episode to test:
```
/dl 274 1
```

### Download first 5 episodes:
```
/dl 274 1-5
```

### Download all episodes:
```
/dl 274
```

### Check what's running:
```
/queue
```

### Stop current download:
```
/cancel
```

## 🚀 That's It!

Simple, clean, and powerful. Enjoy! 🎉
