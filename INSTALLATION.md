# Installation über HACS

Dieses Repository enthält die optionale Home-Assistant-Begleitintegration für
die UniFi Device Card. Es wird in HACS als **Integration** hinzugefügt. Die
eigentliche Karte bleibt ein separates **Dashboard**-Repository.

## Voraussetzungen

- Die offizielle **UniFi Network**-Integration ist in Home Assistant bereits
  eingerichtet und geladen.
- Die **UniFi Device Card** ist als Dashboardkarte installiert.

## Installation

1. HACS in Home Assistant öffnen.
2. **Integrationen** öffnen.
3. Rechts oben **⋮ → Benutzerdefinierte Repositories** auswählen.
4. Als Repository eintragen:
   `https://github.com/RAFd3v-HA/HACS-Unifi-Card-Backend`
5. Kategorie **Integration** auswählen und hinzufügen.
6. Nach **UniFi Device Card Backend** suchen und **Herunterladen** wählen.
7. Home Assistant neu starten.
8. **Einstellungen → Geräte & Dienste → Integration hinzufügen** öffnen und
   **UniFi Device Card Backend** einmal hinzufügen.
9. Die Dashboard-Seite neu laden.

Es werden keine zusätzlichen UniFi-Zugangsdaten abgefragt. Das Backend nutzt
die bereits geladene Verbindung der offiziellen UniFi-Network-Integration.

Wenn das Backend fehlt oder vorübergehend nicht verfügbar ist, verwendet die
Karte weiterhin automatisch ihre bisherige Entity-basierte Erkennung.
