# vynst (inwendo ERP) für Home Assistant

Deutsch | [English](README.md)

Eine Home-Assistant-Integration, die die Buchungskalender aus vynst (inwendo ERP) in Home Assistant bringt. Auf Wunsch verbindet sie außerdem Schlösser in beide Richtungen und ergänzt Belegungssensoren, Zutritts- und Check-in-Ereignisse, Verbrauchszähler und Bilder für Raum-Displays.

Schritt-für-Schritt-Anleitungen mit Bildern stehen in der Hilfe von vynst (inwendo ERP) unter **Smart Home**: Home Assistant verbinden, Schloss mit der App öffnen, Heizung und Licht nach Raumbelegung.

## Funktionen

- Findet die buchbaren Räume und Objekte Ihrer Instanz von vynst (inwendo ERP) automatisch
- Legt für jeden Raum eine Kalender-Entität an
- Webhook: Änderungen an Buchungen kommen sofort an
- Abfrage alle 15 Minuten als Rückfallebene
- Optional: Smart Locks aus vynst (inwendo ERP) (Nuki, LOQED, …) als Schloss-Entitäten in Home Assistant
- Optional: Schlösser aus Home Assistant an vynst (inwendo ERP) freigeben; dessen Nutzer öffnen sie nach den Zutrittsregeln von vynst (inwendo ERP)
- Optional: Raum-Displays (Türschilder) aus vynst (inwendo ERP) auf [OpenDisplay](https://opendisplay.org)-E-Paper-Panels, mit Meldung von Akku und Ausfall zurück an vynst (inwendo ERP), und als Bild-Entitäten für Dashboards
- Optional: Belegungssensoren je Raum (belegt, Vorlaufzeit, geöffnet, nächste Buchung, Teilnehmende)
- Optional: Ereignisse `iw_erp_access` / `iw_erp_check_in`, Geräte-Auslöser, Sensoren „Letzter Zutritt“ und Logbuch-Einträge
- Optional: Verbrauchszähler aus vynst (inwendo ERP) als Sensoren, Home-Assistant-Zähler melden ihre Werte dorthin
- Dienste `iw_erp_homeassistant.check_access` (Karten-, QR- und Tastatur-Leser mit den Zutrittsregeln von vynst (inwendo ERP)) und `iw_erp_homeassistant.create_ticket`
- Blaupausen: Raum zu Buchungen vorheizen, Licht zu Buchungen, Frostschutz, Heizung und Licht nach Raumbelegung
- Reparaturhinweise (keine externe URL, abgelehnter API-Schlüssel, fehlender Bereich) und Diagnose-Download

Alle Texte der Integration (Einrichtung, Optionen, Meldungen, Dienste) gibt es auf Deutsch und Englisch; Home Assistant zeigt sie in seiner eingestellten Sprache. Blaupausen kann Home Assistant nicht übersetzen, sie tragen darum beide Sprachen im Namen und in den Beschreibungen.

## Voraussetzungen

- Home Assistant 2025.1 oder neuer (getestet mit 2026.11)
- Eine Version von vynst (inwendo ERP) mit den Schnittstellen `/api/homeassistant/*`; die Schloss-Funktionen brauchen die Schnittstellen `smart_locks` / `ha_locks`
- Die Funktionen ab Version 1.4 müssen in vynst (inwendo ERP) vorhanden **und** in den Einstellungen eingeschaltet sein (siehe [Einstellungen in vynst (inwendo ERP)](#einstellungen-in-vynst-inwendo-erp)). Ist eine Funktion dort ausgeschaltet, schreibt die Integration eine Info-Zeile ins Protokoll und lässt sie weg – ohne Fehler und ohne neue Anmeldung.

## Installation

### Home Assistant Community Store (HACS, empfohlen)

1. In Home Assistant **HACS** öffnen
2. Nach **vynst** suchen und **vynst (inwendo ERP)** öffnen
3. Auf **Herunterladen** klicken und Home Assistant neu starten

[![In HACS öffnen](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=inwendo&repository=iw_erp_homeassistant&category=integration)

Updates erscheinen in HACS wie bei jeder anderen Integration. Die Versionen sind die [GitHub-Releases](https://github.com/inwendo/iw_erp_homeassistant/releases) dieses Repositorys.

### Von Hand

1. Den Ordner `custom_components/iw_erp_homeassistant` nach `config/custom_components/` von Home Assistant kopieren
2. Home Assistant neu starten

## Einrichtung

1. **Einstellungen** > **Geräte & Dienste** > **Integration hinzufügen**
2. Nach „vynst“ suchen
3. Die Adresse von vynst (inwendo ERP) eingeben (z. B. `https://ihr-verein.iw-erp.de`)
4. Den API-Schlüssel (JWT) eingeben

Lehnt vynst (inwendo ERP) den API-Schlüssel später ab (abgelaufen oder widerrufen), fragt Home Assistant nach einem neuen (**Erneut anmelden**).

### API-Schlüssel

Den API-Schlüssel in der Verwaltung von vynst (inwendo ERP) unter **Einstellungen › API-Zugänge › Hinzufügen › Home Assistant** anlegen. Diese Vorlage setzt die nötigen Bereiche und beschränkt den Schlüssel auf `/api/homeassistant/.*`:

| Bereich | Nötig für |
|---|---|
| **Location** (Lesen) | Räume, Belegungssensoren, Raum-Displays, Verbrauchszähler lesen |
| **Location** (Schreiben) | *Verbrauchszähler*: Werte aus Home Assistant melden |
| **Event Booking** (Lesen + Schreiben) | Kalender und Webhook-Anmeldung (auch Zutritts- und Check-in-Ereignisse) |
| **Event** (Schreiben) | *Schlösser an vynst (inwendo ERP) freigeben*, Dienst `check_access` |
| **Ticket** (Schreiben) | Dienst `create_ticket` |

Fehlt einer eingeschalteten Funktion ein Bereich, erscheint ein Hinweis unter **Einstellungen > System > Reparaturen** (HTTP 403), keine neue Anmeldung. Für *Smart Locks aus vynst (inwendo ERP) importieren* darf der Schlüssel nicht auf einen einzelnen Pfad beschränkt sein.

## Webhook

Bei der Einrichtung meldet die Integration einen Webhook bei vynst (inwendo ERP) an. Ändert sich dort eine Buchung, schickt vynst (inwendo ERP) die Änderung sofort an Home Assistant.

vynst (inwendo ERP) nimmt nur Webhook-Adressen mit `https://` und einem öffentlichen Hostnamen an. Home Assistant braucht also eine externe URL (**Einstellungen > System > Netzwerk** oder Home Assistant Cloud). Ohne sie fragt die Integration weiter regelmäßig ab, und ein Reparaturhinweis sagt das; er verschwindet, sobald der Webhook angemeldet ist.

Technische Einzelheiten (Webhook-Token, `verified`, Aufbau der Nachrichten) stehen in der [englischen Fassung](README.md#webhook).

## Optionen

Alle Funktionen sind zuerst aus. Einschalten unter **Einstellungen > Geräte & Dienste > vynst (inwendo ERP) > Konfigurieren**:

| Option | Was sie ergänzt |
|---|---|
| Smart Locks aus vynst (inwendo ERP) importieren | Schloss-Entitäten für die Smart Locks aus vynst (inwendo ERP) |
| Diese Home-Assistant-Schlösser an vynst (inwendo ERP) freigeben | vynst (inwendo ERP) kann die gewählten Schlösser öffnen |
| Belegungssensoren | Sensoren je Raum |
| Zutritts- und Check-in-Ereignisse | Ereignisse `iw_erp_access` / `iw_erp_check_in`, Sensoren „Letzter Zutritt“, Geräte-Auslöser, Logbuch |
| Verbrauchszähler | Verbrauchszähler als Sensoren; eine zweite Seite ordnet Home-Assistant-Zähler zu |
| Bilder der Raum-Displays | Bild-Entitäten für alle Raum-Displays (Displays mit OpenDisplay-Panel bekommen immer eine) |
| Raum-Displays (Seite erscheint mit der OpenDisplay-Integration) | OpenDisplay-Panels zuordnen |

### Einstellungen in vynst (inwendo ERP)

Unter **Einstellungen › Smart Home** in der Verwaltung:

| Einstellung | Funktion |
|---|---|
| `integration_occupancy_api_enabled` | Belegungssensoren |
| `integration_ha_access_events_enabled` | Zutritts- und Check-in-Ereignisse |
| `integration_ha_access_check_enabled` | Dienst `check_access` |
| `integration_ha_ticket_enabled` | Dienst `create_ticket` |
| `integration_usage_counter_api_enabled` | Verbrauchszähler |

## Belegungssensoren (Option)

Für jeden Raum, den vynst (inwendo ERP) unter `/api/homeassistant/occupancy` meldet (jede Minute abgefragt und sofort nach jedem Buchungs-Webhook):

| Entität (deutsches Home Assistant) | Zustand |
|---|---|
| `binary_sensor.<raum>_belegt` | eine Buchung läuft gerade |
| `binary_sensor.<raum>_vorlauf` | jetzt innerhalb der Vorlaufzeit vor der nächsten Buchung |
| `binary_sensor.<raum>_geoffnet` | geöffnet (nur bei Räumen mit Öffnungszeiten) |
| `sensor.<raum>_nachste_buchung` | Beginn der nächsten Buchung (14 Tage voraus) |
| `sensor.<raum>_teilnehmende` | Teilnehmende der laufenden Buchung |

Die Entitäts-IDs folgen der Sprache von Home Assistant; am sichersten wählt man sie in der Entitätsauswahl. Die Vorlaufzeit ist `automation_pre_start_minutes` des Raums in vynst (inwendo ERP), sonst 30 Minuten. Jeder Raum ist ein Gerät, sein Kalender gehört zum selben Gerät.

## Zutritts- und Check-in-Ereignisse (Option)

vynst (inwendo ERP) meldet jeden Eintrag im Smart-Lock-Protokoll und jeden Check-in und Check-out. Daraus werden die Ereignisse `iw_erp_access` und `iw_erp_check_in`, Sensoren „Letzter Zutritt“, Logbuch-Einträge und Geräte-Auslöser für Automationen. Die Felder der Ereignisse stehen in der [englischen Fassung](README.md#access-and-check-in-events-opt-in).

## Dienste

- `iw_erp_homeassistant.check_access`: fragt vynst (inwendo ERP), ob ein Nachweis (Karten-UID, QR-Code, Tastatur-Code) ein Smart Lock öffnet, und öffnet es auf Wunsch.
- `iw_erp_homeassistant.create_ticket`: legt in vynst (inwendo ERP) ein Ticket an, z. B. bei einem Wasseralarm; mit einem Dedupe-Schlüssel nur eines, solange es offen ist.

Beide Dienste gibt es immer; ist die Funktion in vynst (inwendo ERP) ausgeschaltet, schlagen sie mit einer klaren Meldung fehl. Felder und Antworten: [englische Fassung](README.md#services).

## Blaupausen

| Blaupause | Import |
|---|---|
| Raum zu Buchungen vorheizen (Kalender) | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Fpreheat_room.yaml) |
| Licht zu Buchungen (Kalender) | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Fbooking_light.yaml) |
| Frostschutz außerhalb von Buchungen und Öffnungszeiten | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Ffrost_protection.yaml) |
| Raum nach Belegung heizen (Belegungssensoren) | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Foccupancy_heating.yaml) |
| Licht nach Raumbelegung (Belegungssensoren) | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Finwendo%2Fiw_erp_homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fiw_erp%2Foccupancy_light.yaml) |

Oder **Einstellungen > Automationen & Szenen > Blueprints > Blueprint importieren** mit der GitHub-Adresse der Datei.

Der Kalender-Auslöser von Home Assistant sieht nur Termine, die etwa 15 Minuten vor dem Auslösen schon bestanden. Eine kurzfristige Buchung oder Absage verpassen die Kalender-Blaupausen darum. Die beiden Blaupausen nach Belegung nutzen die Belegungssensoren (Option **Belegungssensoren**, Einstellung `integration_occupancy_api_enabled`) und folgen jeder Änderung innerhalb einer Minute – sie sind die bessere Wahl.

## Schlösser (Option)

Beide Richtungen sind zuerst aus.

- **Smart Locks aus vynst (inwendo ERP) importieren:** eine Schloss-Entität je Smart Lock, das der Nutzer des API-Schlüssels verwenden darf (dieselben Regeln wie in den Apps von vynst (inwendo ERP)). Öffnen und Schließen laufen über vynst (inwendo ERP), das die Rechte prüft und protokolliert.
- **Schlösser an vynst (inwendo ERP) freigeben:** Die gewählten Home-Assistant-Schlösser erscheinen in vynst (inwendo ERP) als SmartLock-Verbindung. **Erst wenn ein Administrator dort „Geräte synchronisieren“ wählt** und die Schlösser Räumen, Nutzern oder Verträgen zuordnet, kann jemand sie öffnen. vynst (inwendo ERP) schickt dann signierte Befehle (HMAC-SHA256, höchstens zwei Minuten alt, nicht wiederholbar) an den Webhook; nur die gewählten Schlösser werden angenommen. Braucht die externe `https://`-Adresse (siehe *Webhook*).

## Raum-Displays auf OpenDisplay-Panels (Option)

Mit der [OpenDisplay-Integration](https://github.com/OpenDisplay/Home_Assistant_Integration) überträgt Home Assistant die Türschilder aus vynst (inwendo ERP) per Bluetooth auf E-Paper-Panels und meldet Akku, Signal und letzten Kontakt zurück. Einrichtung: in vynst (inwendo ERP) ein Raum-Display vom Typ **OpenDisplay** anlegen, dann in den Optionen der Integration auf der zweiten Seite das Panel zuordnen. Einzelheiten: [englische Fassung](README.md#room-displays-on-opendisplay-panels-opt-in).

## Reparaturen und Diagnose

Hinweise unter **Einstellungen > System > Reparaturen**: keine externe URL, API-Schlüssel abgelehnt, Bereich fehlt. **Diagnose herunterladen** (Menü der Integration) enthält Optionen, Zustand jeder Funktion und die letzten Antworten von vynst (inwendo ERP); Schlüssel, Geheimnisse, Buchungstitel und Namen sind geschwärzt.

## Entwicklung

Siehe [englische Fassung](README.md#development).
