#!/usr/bin/env python3
"""Aero Calendar: a Frutiger Aero calendar popup for waybar.

    aero-calendar.py            open the popup; if it's already open, close it
    aero-calendar.py --connect  link Google Calendar (one-time, opens a browser)

Events are kept locally until Google Calendar is linked; after that, new
events go to your primary Google calendar, and local ones are still shown.

Files:
    ~/.config/aero-calendar/credentials.json   Google OAuth client (you download it)
    ~/.config/aero-calendar/token.json         created by --connect
    ~/.local/share/aero-calendar/events.json   local events
"""
import calendar
import datetime as dt
import json
import locale
import os
import sys
import threading
import uuid

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "aero-calendar")
DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "aero-calendar")
CREDS = os.path.join(CONF, "credentials.json")
TOKEN = os.path.join(CONF, "token.json")
LOCAL = os.path.join(DATA, "events.json")
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

PANEL_WIDTH = 438  # fixed, so the popup never resizes (and jumps) while you click around
BAR_OFFSET = 0  # Hyprland already places us below the bar's exclusive zone; #panel margin adds the gap
INSTALL_HINT = "sudo pacman -S python-google-api-python-client python-google-auth-oauthlib"

try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass


def local_tz():
    return dt.datetime.now().astimezone().tzinfo


def cap(s):
    return s[:1].upper() + s[1:]


# ── storage ──────────────────────────────────────────────────────
class LocalStore:
    """Events in a small JSON file: [{id, date, time|null, title}]."""

    def _load(self):
        try:
            with open(LOCAL) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self, events):
        os.makedirs(DATA, exist_ok=True)
        tmp = LOCAL + ".tmp"
        with open(tmp, "w") as f:
            json.dump(events, f, indent=2, ensure_ascii=False)
        os.replace(tmp, LOCAL)

    def fetch(self, start, end):
        out = []
        for e in self._load():
            d = dt.date.fromisoformat(e["date"])
            if start <= d < end:
                out.append({"id": e["id"], "source": "local", "date": d,
                            "time": e.get("time"), "title": e["title"]})
        return out

    def add(self, day, time, title):
        events = self._load()
        events.append({"id": uuid.uuid4().hex, "date": day.isoformat(),
                       "time": time.strftime("%H:%M") if time else None, "title": title})
        self._save(events)

    def delete(self, event_id):
        self._save([e for e in self._load() if e["id"] != event_id])


class Google:
    """Primary Google calendar through the Calendar API (optional)."""

    @staticmethod
    def libs_ok():
        try:
            import google.oauth2.credentials  # noqa: F401
            import googleapiclient  # noqa: F401
            return True
        except ImportError:
            return False

    def state(self):
        if not self.libs_ok():
            return "missing-libs"
        if not os.path.exists(TOKEN):
            return "not-connected"
        return "connected"

    def _service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                write_private(TOKEN, creds.to_json())
            else:
                raise RuntimeError("Google sign-in expired: run aero-calendar.py --connect")
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def fetch(self, start, end):
        tz = local_tz()
        svc = self._service()
        out, page = [], None
        while True:
            resp = svc.events().list(
                calendarId="primary",
                timeMin=dt.datetime.combine(start, dt.time(), tz).isoformat(),
                timeMax=dt.datetime.combine(end, dt.time(), tz).isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=250, pageToken=page,
            ).execute()
            for it in resp.get("items", []):
                if it.get("status") == "cancelled":
                    continue
                title = it.get("summary") or "(no title)"
                s, e = it["start"], it["end"]
                if "date" in s:  # all-day, possibly spanning several days
                    d, last = dt.date.fromisoformat(s["date"]), dt.date.fromisoformat(e["date"])
                    while d < last:
                        if start <= d < end:
                            out.append({"id": it["id"], "source": "google", "date": d,
                                        "time": None, "title": title})
                        d += dt.timedelta(days=1)
                else:
                    t = dt.datetime.fromisoformat(s["dateTime"]).astimezone(tz)
                    out.append({"id": it["id"], "source": "google", "date": t.date(),
                                "time": t.strftime("%H:%M"), "title": title})
            page = resp.get("nextPageToken")
            if not page:
                return out

    def add(self, day, time, title):
        if time:
            begin = dt.datetime.combine(day, time, local_tz())
            body = {"summary": title,
                    "start": {"dateTime": begin.isoformat()},
                    "end": {"dateTime": (begin + dt.timedelta(hours=1)).isoformat()}}
        else:
            body = {"summary": title,
                    "start": {"date": day.isoformat()},
                    "end": {"date": (day + dt.timedelta(days=1)).isoformat()}}
        self._service().events().insert(calendarId="primary", body=body).execute()

    def delete(self, event_id):
        self._service().events().delete(calendarId="primary", eventId=event_id).execute()


def write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def connect():
    if not Google.libs_ok():
        print(f"Google libraries are missing. Install them with:\n  {INSTALL_HINT}")
        return 1
    if not os.path.exists(CREDS):
        print(f"Missing {CREDS}\n"
              "Create an OAuth client (type: Desktop app) in Google Cloud Console with the\n"
              "Google Calendar API enabled, download its JSON and save it at that path.")
        return 1
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = InstalledAppFlow.from_client_secrets_file(CREDS, SCOPES).run_local_server(port=0)
    write_private(TOKEN, creds.to_json())
    print("Google Calendar linked. Reopen the calendar popup to sync.")
    return 0


# ── UI ───────────────────────────────────────────────────────────
def button(label, *classes, tooltip=None):
    b = Gtk.Button(label=label)
    b.set_can_focus(False)
    for c in classes:
        b.get_style_context().add_class(c)
    if tooltip:
        b.set_tooltip_text(tooltip)
    return b


def set_class(widget, name, on):
    ctx = widget.get_style_context()
    (ctx.add_class if on else ctx.remove_class)(name)


class CalendarWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app, title="Aero Calendar")
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "aero-calendar")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, BAR_OFFSET)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.local, self.google = LocalStore(), Google()
        self.events = {}        # date -> [event]
        self.generation = 0     # discards results from outdated fetches
        self.google_error = None
        self.busy = False

        self.connect("key-press-event", self.on_key)
        self.connect("delete-event", lambda *_: self.hide() or True)
        self.build()
        self.go_today()

    # layout
    def build(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, name="panel")
        panel.set_size_request(PANEL_WIDTH, -1)
        self.add(panel)

        header = Gtk.Box(spacing=8, name="header")
        prev_b = button("‹", "orb", tooltip="Previous month")
        next_b = button("›", "orb", tooltip="Next month")
        prev_b.connect("clicked", lambda *_: self.shift_month(-1))
        next_b.connect("clicked", lambda *_: self.shift_month(+1))
        self.month_label = Gtk.Label(name="month", xalign=0, ellipsize=3)
        today_b = button("Today", "pill")
        today_b.connect("clicked", lambda *_: self.go_today())
        header.pack_start(prev_b, False, False, 0)
        header.pack_start(self.month_label, True, True, 4)
        header.pack_start(today_b, False, False, 0)
        header.pack_start(next_b, False, False, 0)
        panel.pack_start(header, False, False, 0)

        grid = Gtk.Grid(name="grid", row_spacing=5, column_spacing=5,
                        column_homogeneous=True, row_homogeneous=False)
        self.cal = calendar.Calendar(firstweekday=0)
        for col, wd in enumerate(self.cal.iterweekdays()):
            lbl = Gtk.Label(label=cap(calendar.day_abbr[wd])[:3])
            lbl.get_style_context().add_class("weekday")
            grid.attach(lbl, col, 0, 1, 1)
        self.cells = []
        for i in range(42):
            b = Gtk.Button()
            b.set_can_focus(False)
            b.get_style_context().add_class("day")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            num, dots = Gtk.Label(), Gtk.Label()
            num.get_style_context().add_class("num")
            dots.get_style_context().add_class("dots")
            box.pack_start(num, True, True, 0)
            box.pack_start(dots, False, False, 0)
            b.add(box)
            b.connect("clicked", self.on_day_clicked, i)
            grid.attach(b, i % 7, 1 + i // 7, 1, 1)
            self.cells.append((b, num, dots))
        panel.pack_start(grid, False, False, 0)

        dayview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, name="dayview")
        self.day_label = Gtk.Label(name="daytitle", xalign=0, ellipsize=3)
        dayview.pack_start(self.day_label, False, False, 0)

        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, name="eventlist")
        scroll = Gtk.ScrolledWindow(name="eventscroll")
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(118)
        scroll.set_max_content_height(118)
        scroll.add(self.list)
        dayview.pack_start(scroll, False, False, 0)

        add_row = Gtk.Box(spacing=6, name="addrow")
        self.time_entry = Gtk.Entry(placeholder_text="HH:MM", width_chars=8, max_length=5)
        self.time_entry.set_tooltip_text("Leave empty for an all-day event")
        self.title_entry = Gtk.Entry(placeholder_text="New event…")
        self.title_entry.connect("activate", self.on_add)
        self.time_entry.connect("activate", lambda *_: self.title_entry.grab_focus())
        add_b = button("Add", "pill", "accent")
        add_b.connect("clicked", self.on_add)
        add_row.pack_start(self.time_entry, False, False, 0)
        add_row.pack_start(self.title_entry, True, True, 0)
        add_row.pack_start(add_b, False, False, 0)
        dayview.pack_start(add_row, False, False, 0)
        panel.pack_start(dayview, True, True, 0)

        footer = Gtk.Box(spacing=6, name="footer")
        self.status = Gtk.Label(name="status", xalign=0, ellipsize=3)  # 3 = END
        refresh_b = button("⟳", "orb", "small", tooltip="Sync now")
        refresh_b.connect("clicked", lambda *_: self.refresh())
        footer.pack_start(self.status, True, True, 0)
        footer.pack_start(refresh_b, False, False, 0)
        panel.pack_start(footer, False, False, 0)

    # navigation
    def go_today(self):
        today = dt.date.today()
        self.month, self.selected = today.replace(day=1), today
        self.refresh()

    def shift_month(self, delta):
        y, m = divmod(self.month.month - 1 + delta, 12)
        self.month = dt.date(self.month.year + y, m + 1, 1)
        last = calendar.monthrange(self.month.year, self.month.month)[1]
        self.selected = self.month.replace(day=min(self.selected.day, last))
        self.refresh()

    def grid_days(self):
        first = self.month - dt.timedelta(days=(self.month.weekday() - self.cal.firstweekday) % 7)
        return [first + dt.timedelta(days=i) for i in range(42)]

    def on_day_clicked(self, _b, i):
        day = self.grid_days()[i]
        self.selected = day
        if (day.year, day.month) != (self.month.year, self.month.month):
            self.month = day.replace(day=1)
            self.refresh()
        else:
            self.render()
        self.title_entry.grab_focus()

    def on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    # data
    def refresh(self):
        self.generation += 1
        gen, days = self.generation, self.grid_days()
        start, end = days[0], days[-1] + dt.timedelta(days=1)
        self.events = {}
        self._merge(self.local.fetch(start, end))
        self.google_error = None
        self.render()
        if self.google.state() == "connected":
            self.set_busy(True)

            def work():
                try:
                    result, err = self.google.fetch(start, end), None
                except Exception as e:  # network, auth, quota…
                    result, err = [], e
                GLib.idle_add(self._google_done, gen, result, err)

            threading.Thread(target=work, daemon=True).start()

    def _google_done(self, gen, result, err):
        if gen != self.generation:
            return False
        self.google_error = err
        self._merge(result)
        self.set_busy(False)
        self.render()
        return False

    def _merge(self, events):
        for e in events:
            self.events.setdefault(e["date"], []).append(e)
        for lst in self.events.values():
            lst.sort(key=lambda e: (e["time"] is not None, e["time"] or ""))

    def run_google(self, fn, *args):
        """Run a Google write off the UI thread, then resync."""
        self.set_busy(True)

        def work():
            try:
                fn(*args)
                err = None
            except Exception as e:
                err = e
            GLib.idle_add(self._write_done, err)

        threading.Thread(target=work, daemon=True).start()

    def _write_done(self, err):
        self.set_busy(False)
        if err:
            self.google_error = err
            self.render_status()
        else:
            self.refresh()
        return False

    def on_add(self, *_):
        title = self.title_entry.get_text().strip()
        raw = self.time_entry.get_text().strip()
        time = None
        if raw:
            try:
                time = dt.datetime.strptime(raw, "%H:%M").time()
            except ValueError:
                set_class(self.time_entry, "error", True)
                return
        set_class(self.time_entry, "error", False)
        if not title:
            self.title_entry.grab_focus()
            return
        self.title_entry.set_text("")
        self.time_entry.set_text("")
        if self.google.state() == "connected":
            self.run_google(self.google.add, self.selected, time, title)
        else:
            self.local.add(self.selected, time, title)
            self.refresh()

    def on_delete(self, _b, event):
        if event["source"] == "google":
            self.run_google(self.google.delete, event["id"])
        else:
            self.local.delete(event["id"])
            self.refresh()

    # rendering
    def set_busy(self, busy):
        self.busy = busy
        self.render_status()

    def render(self):
        today = dt.date.today()
        self.month_label.set_text(cap(self.month.strftime("%B %Y")))
        for (b, num, dots), day in zip(self.cells, self.grid_days()):
            n = len(self.events.get(day, []))
            num.set_text(str(day.day))
            dots.set_text("•" * min(n, 3) or " ")
            set_class(b, "other-month", day.month != self.month.month)
            set_class(b, "today", day == today)
            set_class(b, "selected", day == self.selected)
            set_class(b, "weekend", day.weekday() >= 5)
            b.set_tooltip_text("\n".join(
                f"{e['time'] or 'All day'}  {e['title']}" for e in self.events.get(day, [])) or None)

        self.day_label.set_text(cap(self.selected.strftime("%A, %d %B")))
        for child in self.list.get_children():
            child.destroy()
        evs = self.events.get(self.selected, [])
        if not evs:
            empty = Gtk.Label(label="No events: add one below", xalign=0)
            empty.get_style_context().add_class("empty")
            self.list.pack_start(empty, False, False, 0)
        for e in evs:
            row = Gtk.Box(spacing=8)
            row.get_style_context().add_class("event")
            row.get_style_context().add_class(e["source"])
            t = Gtk.Label(label=e["time"] or "All day", xalign=0)
            t.get_style_context().add_class("time")
            title = Gtk.Label(label=e["title"], xalign=0, ellipsize=3)
            title.get_style_context().add_class("title")
            x = button("✕", "delete", tooltip="Delete event")
            x.connect("clicked", self.on_delete, e)
            row.pack_start(t, False, False, 0)
            row.pack_start(title, True, True, 0)
            row.pack_start(x, False, False, 0)
            self.list.pack_start(row, False, False, 0)
        self.list.show_all()
        self.render_status()

    def render_status(self):
        state = self.google.state()
        if self.busy:
            text, cls = "Syncing with Google Calendar…", "busy"
        elif self.google_error:
            text, cls = f"Google sync failed: {self.google_error}", "error"
        elif state == "connected":
            text, cls = "● Google Calendar linked", "ok"
        elif state == "missing-libs":
            text, cls = "○ Local events only: Google libraries not installed", "off"
        else:
            text, cls = "○ Local events only: run --connect to link Google", "off"
        self.status.set_text(text)
        self.status.set_tooltip_text(str(self.google_error) if self.google_error else None)
        for c in ("busy", "error", "ok", "off"):
            set_class(self.status, c, c == cls)


class App(Gtk.Application):
    def __init__(self):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id="dev.aero.Calendar", flags=flags)
        self.win = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

    def do_activate(self):
        # first launch opens the popup; launching again (clicking the clock) toggles it
        if self.win is None:
            self.win = CalendarWindow(self)
            self.win.show_all()
        elif self.win.get_visible():
            self.win.hide()
        else:
            self.win.go_today()
            self.win.show_all()


if __name__ == "__main__":
    if "--connect" in sys.argv[1:]:
        sys.exit(connect())
    sys.exit(App().run([sys.argv[0]]))
