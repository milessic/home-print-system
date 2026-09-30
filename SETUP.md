# Setup — Home Printing System (Linux Mint)

Three app files: `main.py`, `users.py`, `index.html`.
They expect to live in the same folder, e.g. `/opt/print-system/`.

## 1. System packages (for CUPS + pycups build)

```bash
sudo apt update
sudo apt install -y cups libcups2-dev python3-pip python3-venv
```

Add your user to the `lpadmin` group so you can manage printers/jobs:

```bash
sudo usermod -aG lpadmin $USER
# log out/in for the group change to take effect
```

## 2. Add your printer over WiFi in CUPS

1. Make sure the printer is joined to your WiFi network (use its control
   panel, WPS, or the manufacturer's setup utility once, from any machine).
2. Open `http://localhost:631` (CUPS web UI) on the server, or use the CLI:

   ```bash
   # Find it on the network:
   lpinfo -v | grep -i dnssd

   # Add it (adjust the URI from lpinfo, driverless/AirPrint works well):
   sudo lpadmin -p your_printer_queue -E \
     -v "<uri from lpinfo -v>" \
     -m everywhere
   ```

   If `-m everywhere` (driverless/IPP Everywhere) doesn't work for your
   model, install the manufacturer's official Linux driver package,
   then re-run `lpadmin` pointing at that driver.

3. Test it directly:

   ```bash
   lp -d your_printer_queue /etc/hostname   # should print a test page
   lpstat -p your_printer_queue -l          # check status/reasons
   ```

4. **The queue name must match** the `PRINTER_NAME` environment variable
   used by `main.py` (see below) — `main.py` requires it to be set and
   will refuse to start otherwise.

## 3. Python environment

```bash
cd /opt/print-system
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If `pip install pycups` fails, double check `libcups2-dev` is installed
(step 1) — pycups compiles against the CUPS headers.

## 4. Create users

```bash
source venv/bin/activate
python3 users.py --add alice hunter2
python3 users.py --add bob   sunshine99
python3 users.py --list
```

This creates `users.json` next to `users.py` (salted+hashed passwords only).

## 5. Run the server

```bash
source venv/bin/activate
export PRINTER_NAME=your_printer_queue   # must match the CUPS queue name from step 2
python3 main.py
```

By default it listens on `0.0.0.0:5000`, so from another device on your
LAN go to `http://<server-ip>:5000`.

### Run it as a systemd service (optional, recommended)

`/etc/systemd/system/print-system.service`:

```ini
[Unit]
Description=Home Printing System
After=network.target cups.service

[Service]
User=youruser
WorkingDirectory=/opt/print-system
Environment=PRINTER_NAME=your_printer_queue
ExecStart=/opt/print-system/venv/bin/python3 main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Before starting the service, make sure the `User=` account in the unit
actually owns the app directory — see **Permission denied on uploads/**
below if you skip this and hit that error.

```bash
sudo chown -R youruser:youruser /opt/print-system
sudo systemctl daemon-reload
sudo systemctl enable --now print-system
```

## Troubleshooting

### Permission denied on uploads/ (when running as a systemd service)

If uploads, deletes, hides, or printing (rotation) fail — and in the UI
you see an error ending in "Please contact Administrator" — check the
service log first:

```bash
sudo journalctl -u print-system -n 50 --no-pager
```

A `PermissionError` there almost always means the directory ownership
doesn't match the systemd `User=` the service runs as. This typically
happens because the app was copied/cloned into `/opt/...` as `root` (e.g.
via `sudo cp` or `sudo git clone`), so `root` owns `uploads/`,
`uploads/processed/` and `uploads_meta.json`, while the service itself
runs as an unprivileged `youruser` per the `[Service]` block above — that
user can't write to files it doesn't own.

Fix it by handing ownership of the whole app directory to the service
user, then restarting:

```bash
sudo chown -R youruser:youruser /opt/print-system
sudo systemctl restart print-system
```

If you'd rather not give that user ownership of the whole checkout (e.g.
it's shared with other services), it's enough to own just the paths the
app writes to:

```bash
sudo mkdir -p /opt/print-system/uploads/processed
sudo chown -R youruser:youruser /opt/print-system/uploads
sudo chown youruser:youruser /opt/print-system/uploads_meta.json 2>/dev/null || true
sudo systemctl restart print-system
```

## Notes on how it works

- **Auth**: no tokens/sessions — the browser sends `Authorization: Basic
  base64(user:pass)` on every API call. The page's own login form stores
  that string in `localStorage` (not a native browser password prompt),
  so it's remembered across reloads but never touches a session/token
  store server-side. Every request is re-checked against `users.json`.
- **Rotation**: PDFs and images are rotated server-side with `pypdf` /
  `Pillow` before being sent to `lp`, so what you see rotated in the
  preview is what comes out of the printer.
- **Status colors**: polled every 5s from CUPS via `pycups`
  (`getPrinters()`), mapped as:
  - 🟢 green — idle/printing, no reported problems
  - 🟡 yellow — printer reachable but reporting a reason (e.g.
    `media-empty`, `toner-low`, `door-open`)
  - 🔴 red — can't reach CUPS, printer not in CUPS, or printer stopped
- Uploaded files live in `uploads/`; rotated copies used just for
  printing live in `uploads/processed/`. Feel free to add a cron job or
  systemd timer to clear old files periodically.
- **Error handling**: any unexpected server-side failure (e.g. a
  filesystem permission error) is logged server-side and returned to the
  browser as a generic HTTP 500; the UI appends "Please contact
  Administrator" to those messages, since there's nothing the end user
  can do about them. Check `journalctl -u print-system` (or the console
  main.py was started from) for the actual cause — see **Permission
  denied on uploads/** above for the most common one.
