# FocusLock

FocusLock helps you stay off distracting websites while you work on your Windows PC.

- **Blocks sites** like YouTube so they won't load while you're focusing.
- **Focus sessions**: pick how you want to work and FocusLock times it.
- **Break reminders**: every 90 minutes a full-screen break reminds you to stretch, walk and drink water.
- **Earn free time**: every 90 minutes of focus earns 10 minutes when the blocked sites are open again.
- **Keeps you honest**: a friend can set a password so you can't just switch it off, and any attempt to get around it is recorded.

Everything stays on your computer. FocusLock doesn't send your data anywhere.

## Install

### Easiest: download the app

1. Go to the **Releases** section on the right side of this page and open the newest release.
2. Under **Assets**, download **FocusLock.exe**.
3. Double-click it. Windows asks "Do you want to allow this app to make changes?": click **Yes**. FocusLock needs this because blocking sites means editing a Windows system file.

**"Windows protected your PC"?** Windows shows this for apps from small developers that aren't paid-certificate signed. Click **More info**, then **Run anyway**.

You can move `FocusLock.exe` anywhere, e.g. your Desktop. Your settings and history are stored separately, so they're kept when you update.

### Or: run it with Python

1. **Install Python** 3.11 or newer from [python.org/downloads](https://www.python.org/downloads/). In the installer, tick **"Add python.exe to PATH"**.
2. **Download FocusLock**: click the green **Code** button on this page, then **Download ZIP**, and unzip it.
3. **Double-click `FocusLock.bat`** in the unzipped folder and click **Yes** when Windows asks for permission.

## Using FocusLock

Use the left menu to move between **Focus**, **Blocklist**, **Rewards**, **Stats** and
**Preferences**. The **View** menu and **Ctrl+1** through **Ctrl+5** do the same.
Preferences includes a **Reduce motion** option for page transitions and a summary
of your current session rules. The motion option applies to the current window.

Only the fields for your chosen mode appear: a duration for Timer, a goal for Goal,
and neither for Flow. During a session, those controls are locked to the active
session. Flow counts up and offers optional break reminders.

### Choose what to block

Open the **Blocklist** tab, type a site (for example `netflix.com`) and click **Add**. Tick **+ www.** to block the `www.` version too. YouTube is blocked from the start.

### Start a focus session

On the **Focus** tab, pick a mode and click **Start**:

| Mode | Best for | How it ends |
|---|---|---|
| **Timer** | "I'll work for 90 minutes" | Automatically when the time is up |
| **Flow** | Working as long as you're in the zone | When you click **Stop**. No forced breaks, just reminders |
| **Goal** | "I'll finish my lab report" | When you click **Goal done** |

There's no pause button. Once you start, you're committed.

### Breaks

During Timer and Goal sessions, a full-screen break appears every 90 minutes of focus and lasts 10 minutes. Sites stay blocked during breaks.

### Spending your free time

Every completed session of at least 25 minutes earns reward time (90 minutes of focus = 10 minutes). On the **Rewards** tab, choose how many minutes to spend and click **Spend**. Blocked sites open until the time runs out, then they're blocked again. You can spend up to 60 minutes a day.

### Your progress

The **Stats** page shows how long you focused each day, your streak of focused days,
and recent protection activity, such as early closes and password overrides.

## Making it hard to cheat

Ask a friend, parent or partner to set a password you don't know. Close the FocusLock window first.

- **If you downloaded the app:** also download **focuslock-cli.exe** from the same release, put it in your Downloads folder, open **Command Prompt** (press the Windows key and type `cmd`) and run:

  ```bat
  cd %USERPROFILE%\Downloads
  focuslock-cli password set
  ```

- **If you use Python:** open Command Prompt in the FocusLock folder and run `python -m focuslock password set`.

Windows asks for permission, a new window opens, and they type the password there. After that:

| You want to... | Needs the password? |
|---|---|
| Unblock all sites, or remove a site from the list | Yes |
| End a **Timer** or **Goal** session early | Yes |
| End a **Flow** session, or mark a goal done | No |
| Add a site, or spend reward time you earned | No |

**Closing FocusLock doesn't help either.** Sites stay blocked, and the session picks up where it left off the next time you open FocusLock.

Every use of the password, every wrong password, every early close and every attempt to edit the blocking by hand goes in the **cheat log** on the Stats tab.

## Good to know

- **A site still loads?** Your browser may be using "secure DNS", which skips FocusLock. In Chrome or Edge, open Settings, search for **secure DNS**, and turn it off. Then close the browser completely and open it again.
- **Settings** (session length, break length, reward rate) are in `settings.json` in `%LOCALAPPDATA%\FocusLock`. Paste that path into File Explorer's address bar to find it. To create the file, run `focuslock-cli init` (or `python -m focuslock init`).
- **Commands instead of the window:** `focuslock-cli --help` (or `python -m focuslock --help`) lists every command.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Windows protected your PC" | Click **More info**, then **Run anyway** |
| "FocusLock is already running" | Close the other FocusLock window first |
| `python` is not recognized | Reinstall Python and tick **"Add python.exe to PATH"** |
| "No module named focuslock" | Run `FocusLock.bat` from inside the unzipped FocusLock folder |
| "the GUI needs tkinter" | Reinstall Python and keep **"tcl/tk and IDLE"** ticked |

## Uninstall

1. Run `focuslock-cli unblock` (or `python -m focuslock unblock`) in Command Prompt. It needs the password if one is set.
2. Delete `FocusLock.exe` (or the FocusLock folder) and the folder `%LOCALAPPDATA%\FocusLock`.

If the password is lost, any Windows administrator can still remove the blocking by deleting the lines between the FocusLock markers in `C:\Windows\System32\drivers\etc\hosts`.
