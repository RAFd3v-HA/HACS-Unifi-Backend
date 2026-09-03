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
9. Die vorhandene offizielle UniFi-Integration als Datenquelle auswählen
   (empfohlen).
10. Die Dashboard-Seite neu laden.

Im empfohlenen Standardmodus werden keine zusätzlichen UniFi-Zugangsdaten
abgefragt. Das Backend nutzt die geladenen Verbindungen der offiziellen
UniFi-Network-Integration und erzeugt keine zweite Controller-Sitzung.

Nur wenn diese Quelle die benötigten Daten nicht bereitstellt, kann bewusst
**Separater UniFi-Login** als Fallback gewählt werden. Der Login und der
gewählte Standort werden gegen den echten Controller geprüft. Zugangsdaten
bleiben ausschließlich im Home-Assistant-Config-Entry und werden niemals an
Karte oder Browser weitergegeben. Für Schreibaktionen wie PoE-Neustart oder
Etherlighting benötigt das UniFi-Konto Administratorrechte.

Wenn der separate Login MFA verlangt, erscheint automatisch ein eigener,
maskierter Schritt. Dort wird der Base32-TOTP-Einrichtungsschlüssel aus der
Authenticator-Einrichtung eingetragen – nicht der aktuelle sechsstellige
Einmalcode. Das Backend speichert diesen Schlüssel im Config-Entry, damit es
sich nach einem Neustart selbstständig neu anmelden kann; Diagnosen enthalten
ihn nicht. Eine reine Ubiquiti-Verify-/Push-Freigabe kann eine unbeaufsichtigte
Sitzung nicht wiederholen. Nutze in diesem Fall die offizielle UniFi-Integration
oder ein separates lokales Konto.

**Konfigurieren** zeigt Quellenstatus und Diagnoseeinstellung. **Neu
konfigurieren** wechselt die Quelle oder erneuert separate Zugangsdaten samt
TOTP-Einrichtungsschlüssel. Das
Backend legt keine Dashboard-Karten an und verändert vorhandene
Lovelace-Konfigurationen nicht automatisch.

Wenn das Backend fehlt oder vorübergehend nicht verfügbar ist, verwendet die
Karte weiterhin automatisch ihre bisherige Entity-basierte Erkennung.

Die LED-Schaltfläche erscheint für Switches und Access Points, sobald die
offizielle UniFi-Integration eine `light.*`-Entity für das Gerät bereitstellt.
Ein PoE-Neustart wird zusätzlich über das Backend angeboten, wenn der Controller
den gewählten Port eindeutig als PoE-fähig und aktiviert meldet; dafür sind
Administratorrechte in Home Assistant und UniFi erforderlich.
Etherlighting wird nur bei Geräten angezeigt, die der UniFi-Controller als
fähig meldet. Änderungen an Etherlighting sind auf Home-Assistant- und UniFi-
Administratoren beschränkt. Meldet die Hardware Etherlighting, aber keine
kompatible Konfiguration, zeigt die Karte einen Diagnosehinweis; nicht
unterstützte Geräte bleiben unverändert.
Der Schreibpfad ist für UniFi Network 10 ab Version 10.5.62 validiert. Bei einer
anderen oder nicht ermittelbaren Network-Version bleibt Etherlighting sichtbar,
aber schreibgeschützt.
