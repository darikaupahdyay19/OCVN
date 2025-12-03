# OceanVeil Bot - Complete Feature Guide

## 🎯 All New Features

### 1. **Unified Progress UI** 📊
Now shows **ONE** progress message for all episodes instead of multiple messages!

**Example:**
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

Then switches to:
```
📤 Uploading 3/7
📁 MyAnime_E03_[Dub].mp4
📦 Size: 560.00 MB
```

### 2. **Episode Selection** 🎯

#### Download All Episodes:
```
/dl 274
```

#### Download Single Episode:
```
/dl 274 5
```
Downloads only episode 5

#### Download Episode Range:
```
/dl 274 1-5
```
Downloads episodes 1 through 5

#### Works with all commands:
```
/engdl 274 3        # Single episode
/engdl 274 1-10     # Range
```

### 3. **Queue Management** 📋

#### Check Queue Status:
```
/queue
```

**Response:**
```
📊 Queue Status

🔄 Active Task:
Downloading My Anime Series

⏳ Queued: 2 tasks
```

#### Cancel Current Task:
```
/cancel
```

**Response:**
```
🛑 Cancelling current task...
```

**Features:**
- Only one task per user at a time
- Prevents starting multiple downloads
- Clean cancellation with proper cleanup
- Shows what's currently running

### 4. **Short, Clean Filenames** 📝

**OLD (Too Long):**
```
Premium_Guilty_Hole_Room_of_Guilty_Pleasure_Episode_3_Congratulations.mp4
```

**NEW (Clean & Short):**
```
Guilty_Hole_E03_[Dub].mp4
```

**Format:**
- `SeriesName_E##_[Type].mp4`
- Removes "Premium", "DubPremium", etc.
- Max 60 characters for series name
- Always under Telegram's limit
- Episode numbers zero-padded (E01, E02, etc.)

**Examples:**
```
MyAnime_E01_[Sub].mp4
MyAnime_E05_[Dub].mp4
MyAnime_E12_[Dual].mp4
```

### 5. **Smart Filename Cleaning** 🧹

Automatically removes:
- ✅ "Premium"
- ✅ "DubPremium"
- ✅ "Dub Premium"
- ✅ Extra spaces
- ✅ Special characters
- ✅ Long titles (truncated intelligently)

### 6. **One Task Per User** 🔒

**Protection against:**
- Multiple simultaneous downloads
- Queue conflicts
- Resource exhaustion

**How it works:**
```
User: /dl 274
Bot: ✅ Starting...

User: /dl 275
Bot: ⚠️ You already have an active task. Use /cancel to stop it first.
```

## 📖 Complete Command Reference

### Basic Commands

#### `/start`
Shows help menu with all commands

#### `/dl <id>`
Download all episodes
```
/dl 274
```

#### `/dl <id> <episode>`
Download single episode
```
/dl 274 5
```

#### `/dl <id> <start>-<end>`
Download episode range
```
/dl 274 1-10
```

#### `/engdl <id>`
Same as `/dl` but for English dub
```
/engdl 274
/engdl 274 3
/engdl 274 1-5
```

### Dual Audio Commands

#### `/dual <id1> <id2>`
Create dual audio (sub video + dub audio)
```
/dual 274 275
```

#### `/engvdiddual <id1> <id2>`
Create dual audio (dub video + sub audio)
```
/engvdiddual 274 275
```

### Queue Commands

#### `/queue`
Show current queue status
```
/queue
```

#### `/cancel`
Cancel current task
```
/cancel
```

## 🎬 Usage Examples

### Example 1: Download Specific Episodes
```
User: /dl 274 1-3
Bot: 🔍 Fetching info for 274...
Bot: ✅ Found 3 episodes
     ⬇️ Starting...

[Shows unified progress for all 3]

Bot: 📤 Uploading 1/3
     📁 MyAnime_E01_[Sub].mp4
     
Bot: 📤 Uploading 2/3
     📁 MyAnime_E02_[Sub].mp4
     
Bot: 📤 Uploading 3/3
     📁 MyAnime_E03_[Sub].mp4

Bot: ✅ Completed!
     📊 Processed 3 episodes
```

### Example 2: Queue Management
```
User: /dl 274
Bot: ✅ Starting download...

[In another chat]
User: /queue
Bot: 📊 Queue Status
     🔄 Active Task:
     Downloading My Anime Series

User: /cancel
Bot: 🛑 Cancelling current task...
Bot: 🛑 Task cancelled by user.
```

### Example 3: Episode Selection
```
# Download only episode 5
User: /dl 274 5
Bot: ✅ Found 1 episode
     [Downloads and uploads only E05]

# Download episodes 1-3
User: /dl 274 1-3
Bot: ✅ Found 3 episodes
     [Downloads and uploads E01, E02, E03]

# Download all
User: /dl 274
Bot: ✅ Found 12 episodes
     [Downloads and uploads all]
```

## 🔥 Key Improvements

### Before:
```
❌ Multiple "Uploading..." messages
❌ Long filenames: "Premium_Long_Title_Episode_3_Name.mp4"
❌ No way to cancel
❌ No queue management
❌ Must download all episodes
```

### After:
```
✅ ONE unified progress message
✅ Short filenames: "Title_E03_[Dub].mp4"
✅ /cancel command
✅ /queue command
✅ Select specific episodes or ranges
✅ Clean, organized workflow
```

## 🎯 Perfect For:

- **Downloading specific episodes** you missed
- **Testing** with single episode before downloading all
- **Managing** multiple download requests
- **Cancelling** long downloads
- **Clean filenames** that fit Telegram limits
- **Organized** file management

## 🚀 Ready to Use!

All features are active and working. Try:
```
/start          # See help
/dl 274 1       # Download episode 1
/queue          # Check status
/cancel         # Cancel if needed
```

Enjoy your clean, organized, and powerful anime bot! 🎉
