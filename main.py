#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pronote → ICS (serveur HTTP minimal)
- Demande les identifiants au terminal (pas d'env, pas de Docker).
- Sert un fichier ICS à l'URL: http://127.0.0.1:8000/calendar.ics
- Paramètres dynamiques: ?weeks=8 (1..26)
- Cache des données Pronote (TTL par défaut: 120 s) pour limiter les connexions.
- UID stables pour permettre les mises à jour/annulations propres côté calendrier.

Dépendances:
    pip install pronotepy icalendar python-dateutil pytz
"""
import hashlib
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
from getpass import getpass

import pytz
from dateutil import tz
from icalendar import Calendar, Event

# --- pronotepy peut être absent: message clair
try:
    import pronotepy
except Exception as e:
    print(e)
    print()
    print("⚠️  Le module 'pronotepy' est requis.\n"
          "Installe-le avec:  pip install pronotepy")
    sys.exit(1)


# ==========================
# Config interactive (terminal)
# ==========================
def prompt_non_empty(label):
    while True:
        v = input(label).strip()
        if v:
            return v
        print("⛔ Entrée vide, réessaie.")

def choose_optional(label, default=""):
    v = input(f"{label} [{default}]: ").strip()
    return v or default

def prompt_int(label, default, min_v, max_v):
    while True:
        s = input(f"{label} [{default}]: ").strip() or str(default)
        try:
            x = int(s)
            if min_v <= x <= max_v:
                return x
            print(f"⛔ Entier entre {min_v} et {max_v} attendu.")
        except:
            print("⛔ Entier invalide, réessaie.")

print("=== Pronote → ICS (serveur local) ===")
PRONOTE_URL = prompt_non_empty("URL Pronote (ex: https://xxxxx.index-education.net/pronote/eleve.html): ")
PRONOTE_USERNAME = prompt_non_empty("Identifiant: ")
PRONOTE_PASSWORD = getpass("Mot de passe (invisible): ")
PRONOTE_ENT = choose_optional("ENT (laisser vide si aucun, ex: EcoleDirecte)", "")
TIMEZONE = choose_optional("Fuseau horaire IANA", "Europe/Paris")
DEFAULT_WEEKS_FORWARD = prompt_int("Nombre de semaines à exposer par défaut (1..26)", 8, 1, 26)
CACHE_TTL_SECONDS = prompt_int("TTL du cache Pronote en secondes (60..900)", 120, 60, 900)
PORT = prompt_int("Port HTTP à utiliser (1024..65535)", 8000, 1024, 65535)

# ==========================
# Backend: Pronote + ICS
# ==========================
LOCAL_TZ = tz.gettz(TIMEZONE)

class PronoteBackend:
    def __init__(self, url, user, pwd, ent_name, local_tz, cache_ttl):
        self.url = url
        self.user = user
        self.pwd = pwd
        self.ent_name = ent_name.strip()
        self.local_tz = local_tz
        self.cache_ttl = cache_ttl

        self._cache_until = 0
        self._cache_range = (None, None)  # (start_date, end_date)
        self._cache_lessons = []

    def _login(self):
        if self.ent_name:
            # ATTENTION: si tu passes par un ENT il faudra mettre le bon constructeur
            raise RuntimeError("Cette version de pronotepy ne gère pas ent_list directement.")
        client = pronotepy.Client(self.url, username=self.user, password=self.pwd)

        if not client.logged_in:
            raise RuntimeError("Échec de connexion (vérifie URL/identifiants).")
        return client


    def _to_local(self, dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self.local_tz)
        return dt.astimezone(self.local_tz)

    def _fetch_lessons(self, start_date, end_date):
        client = self._login()
        return client.timetable(start_date, end_date)

    def get_lessons(self, start_date, end_date):
        now = time.time()
        if (now < self._cache_until
            and self._cache_range[0] == start_date
            and self._cache_range[1] == end_date):
            return self._cache_lessons

        lessons = self._fetch_lessons(start_date, end_date)
        self._cache_lessons = lessons
        self._cache_range = (start_date, end_date)
        self._cache_until = now + self.cache_ttl
        return lessons

    @staticmethod
    def _uid_for(start_dt, title, room, teacher):
        base = f"{start_dt.isoformat()}|{title}|{room}|{teacher}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest() + "@pronote-ics"

    def lessons_to_ics(self, lessons):
        cal = Calendar()
        cal.add("prodid", "-//Pronote ICS//Assia//FR")
        cal.add("version", "2.0")

        for lesson in lessons:
            # Champs robustes (selon versions pronotepy)
            start = self._to_local(getattr(lesson, "start", None))
            end = self._to_local(getattr(lesson, "end", None))
            if not (start and end):
                continue

            title = getattr(lesson, "subject", None) or getattr(lesson, "subject_name", "") or "Cours"
            room = getattr(lesson, "classroom", "") or getattr(lesson, "classroom_name", "") or ""
            teacher = getattr(lesson, "teacher", "") or getattr(lesson, "teacher_name", "") or ""
            group = getattr(lesson, "group_name", "") or ""
            canceled = bool(getattr(lesson, "canceled", False))

            ev = Event()
            ev.add("summary", title if not group else f"{title} ({group})")

            desc_lines = []
            if teacher: desc_lines.append(f"Enseignant·e : {teacher}")
            if room:    desc_lines.append(f"Salle : {room}")
            if canceled: desc_lines.append("⚠️ Séance annulée")
            ev.add("description", "\n".join(desc_lines) if desc_lines else "Séance")

            if room:
                ev.add("location", room)

            # Important: UID stable pour permettre la mise à jour côté agenda
            ev.add("uid", self._uid_for(start, title, room, teacher))
            ev.add("dtstart", start)
            ev.add("dtend", end)
            ev.add("status", "CANCELLED" if canceled else "CONFIRMED")

            cal.add_component(ev)

        return cal.to_ical()


backend = PronoteBackend(
    PRONOTE_URL, PRONOTE_USERNAME, PRONOTE_PASSWORD, PRONOTE_ENT, LOCAL_TZ, CACHE_TTL_SECONDS
)


# ==========================
# Serveur HTTP minimal
# ==========================
class ICSRequestHandler(BaseHTTPRequestHandler):
    def _send_400(self, msg):
        data = msg.encode("utf-8")
        self.send_response(400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_500(self, msg):
        data = msg.encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_ics(self, ics_bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Length", str(len(ics_bytes)))
        self.end_headers()
        self.wfile.write(ics_bytes)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            data = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path not in ("/calendar.ics", "/calendar"):
            self._send_400("Utilise /calendar.ics (option: ?weeks=8)")
            return

        try:
            qs = parse_qs(parsed.query or "")
            weeks = int(qs.get("weeks", [str(DEFAULT_WEEKS_FORWARD)])[0])
            if weeks < 1 or weeks > 26:
                weeks = DEFAULT_WEEKS_FORWARD
        except:
            weeks = DEFAULT_WEEKS_FORWARD

        try:
            # Fenêtre: on inclut la semaine passée pour rafraîchir les annulations tardives
            today = datetime.now(tz=LOCAL_TZ).date()
            start_date = today - timedelta(days=7)
            end_date = today + timedelta(weeks=weeks)

            lessons = backend.get_lessons(start_date, end_date)
            ics_bytes = backend.lessons_to_ics(lessons)
            self._send_ics(ics_bytes)
        except Exception as e:
            self._send_500(f"Erreur génération ICS: {e}")

def run_server(port):
    addr = ("0.0.0.0", port)
    httpd = HTTPServer(addr, ICSRequestHandler)
    print(f"\n🌐 Serveur prêt:  http://127.0.0.1:{port}/calendar.ics  (ou http://<IP>:{port}/calendar.ics)")
    print("↻ Paramètre optionnel: ?weeks=1..26 (ex: /calendar.ics?weeks=12)")
    print("🩺 Santé: /health")
    print("⛔ Quitter avec Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Arrêt…")
    finally:
        httpd.server_close()

if __name__ == "__main__":
    run_server(PORT)
