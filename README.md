# ESP-WROOM-32 · MicroPython quick-command cheatsheet (Windows 11, PowerShell)

| Goal | Command (PowerShell) | Notes |
|------|----------------------|-------|
| **1 · Install Python 3** | Download `python-3.x.y-amd64.exe` → run → ✔ **“Add to PATH”** | Open a **new** PowerShell window after install. |
| **2 · Upgrade pip** (optional) | `python -m pip install --upgrade pip` | — |
| **3 · Install `mpremote` + `esptool`** | `python -m pip install --upgrade mpremote esptool` | `mpremote` = file-manager & REPL · `esptool` = flasher |
| **4 · Find the board’s COM port** | Device Manager → **Ports (COM & LPT)** → e.g. **COM3** | If you use **WebREPL** (Wi-Fi) you can omit `connect COM3` in every command. |
| **5 · Open a USB REPL** | `python -m mpremote connect COM3` | Exit with **Ctrl-\]** or **Ctrl-X**. |
| **6 · Send (copy) a file to `/flash/`** | `python -m mpremote connect COM3 cp main.py :` | `:` = root of ESP flash. Use `-r src/ :` to copy a folder. |
| **7 · Fetch a file PC ← ESP** | `python -m mpremote connect COM3 cp :boot.py .` | Copies into the current PC directory. |
| **8 · Delete a file on the ESP** | `python -m mpremote connect COM3 rm :lib/old.py` | List with `mpremote … ls :` |
| **9 · Soft-reset (run `boot.py` ➜ `main.py`)** | `python -m mpremote connect COM3 reset` | Equivalent to pressing **EN / RST**. |
| **10 · Flash new firmware** (occasional) | ```powershell
python -m esptool --port COM3 erase_flash
python -m esptool --port COM3 write_flash -z 0x1000 micropython.bin
``` | Run only when you want to update MicroPython. |

> **Tip – WebREPL instead of USB**  
> Replace every `connect COM3` with `connect ws:192.168.x.x` (or just omit `connect …` if `mpremote` has the board cached).
