"""Erstellung und Versand der Wochenübersicht (Digest) an den Key Account Manager."""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from p7_akquise.models import Lead

logger = logging.getLogger("p7_akquise.digest")


def generate_digest_html(
    a_leads: list[Lead],
    run_stats: dict[str, Any],
    pending_clarifications: list[Lead] | None = None,
    due_followups: list[Lead] | None = None,
    excel_link: str = "",
) -> str:
    """Generiert eine strukturierte, responsive HTML-E-Mail im projekt7-Layout."""
    rows_html = ""
    for lead in a_leads:
        contact_str = "Nicht ermittelt"
        if lead.kontakt_name:
            contact_str = f"{lead.kontakt_name} ({lead.kontakt_rolle or 'Rolle k.A.'})"
            if lead.kontakt_email:
                contact_str += f"<br><a href='mailto:{lead.kontakt_email}'>{lead.kontakt_email}</a>"

        rows_html += f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 12px; font-weight: bold; color: #1F2A44;">
                {lead.firma}<br>
                <span style="font-size: 12px; font-weight: normal; color: #64748b;">
                    <a href="{lead.website}" target="_blank" style="color: #3b82f6;">{lead.domain}</a> | {lead.ort or ''} ({lead.land})
                </span>
            </td>
            <td style="padding: 12px;">
                <span style="display: inline-block; padding: 4px 8px; background-color: #F59E0B; color: white; border-radius: 4px; font-weight: bold;">
                    Score {lead.score} (Kl. {lead.klasse})
                </span>
            </td>
            <td style="padding: 12px; color: #334155;">
                <strong>{'Produktion' if lead.segment == 'A' else ('Handel' if lead.segment == 'B' else 'andere Branche')}</strong> · {lead.quelle_kanal}<br>
                <span style="font-size: 12px; color: #64748b;">{lead.kaufsignal or ''}</span>
            </td>
            <td style="padding: 12px; color: #334155; font-size: 13px;">
                {contact_str}
            </td>
            <td style="padding: 12px; color: #334155; font-size: 12px;">
                <strong>Empfohlen:</strong> {lead.angebot or 'Abacus ERP'}<br>
                <em>{lead.begruendung[:140]}...</em>
            </td>
        </tr>
        """

    clarifications_html = ""
    if pending_clarifications:
        clarifications_html = f"""
        <div style="background-color: #fff1f2; border-left: 4px solid #F59E0B; padding: 12px; margin-top: 20px; border-radius: 4px;">
            <h4 style="margin: 0 0 8px 0; color: #9f1239;">Achtung: {len(pending_clarifications)} Verwerfung(en) ohne Begründung</h4>
            <p style="margin: 0; font-size: 13px; color: #881337;">
                Folgende Leads wurden mit 'nein' markiert, es fehlt jedoch der Pflichtgrund in der Spalte 'verwerfungsgrund':
            </p>
            <ul style="margin: 8px 0 0 0; padding-left: 20px; font-size: 13px; color: #881337;">
                {"".join(f"<li><strong>{l.firma}</strong> ({l.domain})</li>" for l in pending_clarifications)}
            </ul>
        </div>
        """

    followups_html = ""
    if due_followups:
        followups_html = f"""
        <div style="background-color: #eff6ff; border-left: 4px solid #3b82f6; padding: 12px; margin-top: 20px; border-radius: 4px;">
            <h4 style="margin: 0 0 8px 0; color: #1e40af;">Fällige Nachfass-Aktionen ({len(due_followups)})</h4>
            <p style="margin: 0; font-size: 13px; color: #1e3a8a;">
                Für folgende Leads liegen Nachfass-Entwürfe im Dashboard bereit:
            </p>
            <ul style="margin: 8px 0 0 0; padding-left: 20px; font-size: 13px; color: #1e3a8a;">
                {"".join(f"<li><strong>{l.firma}</strong> ({l.domain}) – Status: {l.status} (Versuch {l.anzahl_nachfass})</li>" for l in due_followups)}
            </ul>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>projekt7 KI-Akquise Wochenübersicht</title>
    </head>
    <body style="font-family: Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 20px; color: #1e293b;">
        <div style="max-width: 900px; margin: 0 auto; background: white; border-radius: 8px; border: 1px solid #cbd5e1; overflow: hidden;">
            <!-- Header -->
            <div style="background-color: #1F2A44; padding: 24px; color: white;">
                <div style="font-size: 24px; font-weight: bold; letter-spacing: 0.5px;">
                    projekt7 <span style="color: #F59E0B;">Lead-Cockpit</span>
                </div>
                <div style="font-size: 14px; opacity: 0.85; margin-top: 4px;">
                    Wochenübersicht Neuer Leads | Lauf-ID: {run_stats.get('lauf_id', 'N/A')}
                </div>
            </div>

            <!-- KPI Tiles -->
            <div style="display: flex; gap: 12px; padding: 20px; background-color: #f1f5f9; border-bottom: 1px solid #cbd5e1;">
                <div style="flex: 1; background: white; padding: 12px; border-radius: 6px; border-left: 4px solid #F59E0B;">
                    <div style="font-size: 12px; color: #64748b; font-weight: bold;">NEUE A-LEADS</div>
                    <div style="font-size: 22px; font-weight: bold; color: #1F2A44;">{run_stats.get('a_leads', len(a_leads))}</div>
                </div>
                <div style="flex: 1; background: white; padding: 12px; border-radius: 6px; border-left: 4px solid #3b82f6;">
                    <div style="font-size: 12px; color: #64748b; font-weight: bold;">B-LEADS BEOBACHTET</div>
                    <div style="font-size: 22px; font-weight: bold; color: #1F2A44;">{run_stats.get('b_leads', 0)}</div>
                </div>
                <div style="flex: 1; background: white; padding: 12px; border-radius: 6px; border-left: 4px solid #10b981;">
                    <div style="font-size: 12px; color: #64748b; font-weight: bold;">NEUE FIRMEN GEPRÜFT</div>
                    <div style="font-size: 22px; font-weight: bold; color: #1F2A44;">{run_stats.get('kandidaten_recherchiert', 0)}</div>
                </div>
                <div style="flex: 1; background: white; padding: 12px; border-radius: 6px; border-left: 4px solid #64748b;">
                    <div style="font-size: 12px; color: #64748b; font-weight: bold;">IMPORT BEWERTET</div>
                    <div style="font-size: 22px; font-weight: bold; color: #1F2A44;">{run_stats.get('import_bewertet', 0)}</div>
                </div>
            </div>

            <!-- Content -->
            <div style="padding: 24px;">
                <h3 style="margin-top: 0; color: #1F2A44;">A-Leads zur Prüfung ({len(a_leads)} Leads)</h3>
                <p style="font-size: 14px; color: #475569;">
                    Bitte die folgenden Leads im Dashboard prüfen und freigeben oder verwerfen.
                </p>

                <table style="width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 14px;">
                    <thead>
                        <tr style="background-color: #f8fafc; border-bottom: 2px solid #cbd5e1; text-align: left; color: #475569;">
                            <th style="padding: 10px;">Unternehmen</th>
                            <th style="padding: 10px;">Bewertung</th>
                            <th style="padding: 10px;">Segment & Signal</th>
                            <th style="padding: 10px;">Ansprechperson</th>
                            <th style="padding: 10px;">Einstieg</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html if rows_html else '<tr><td colspan="5" style="padding: 16px; text-align: center; color: #64748b;">In diesem Lauf wurden keine neuen A-Leads identifiziert.</td></tr>'}
                    </tbody>
                </table>

                {clarifications_html}
                {followups_html}

                <!-- Action Link -->
                <div style="margin-top: 30px; text-align: center; display: {'block' if excel_link else 'none'};">
                    <a href="{excel_link}" style="display: inline-block; background-color: #1F2A44; color: white; text-decoration: none; padding: 12px 24px; border-radius: 6px; font-weight: bold; font-size: 14px;">
                        Zum Dashboard
                    </a>
                </div>
            </div>

            <!-- Footer -->
            <div style="padding: 16px 24px; background-color: #f8fafc; border-top: 1px solid #e2e8f0; font-size: 12px; color: #94a3b8; text-align: center;">
                projekt7 AG · Team Produktion und Handel · Automatische Wochenanalyse
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_digest_email(
    html_content: str,
    subject: str = "projekt7 KI-Akquise: Neue Leads zur Wochenprüfung",
    to_email: str | None = None,
    cc_email: str | None = None,
    dry_run: bool = False,
) -> bool:
    """Sendet die Wochenübersicht per E-Mail oder loggt sie im Dry-Run Modus."""
    to_addr = to_email or os.getenv("DIGEST_TO", "")
    cc_addr = cc_email or os.getenv("DIGEST_CC", "")

    if dry_run:
        logger.info("[Dry-Run] Digest-E-Mail generiert (Empfänger: %s, CC: %s). Kein Versand.", to_addr, cc_addr)
        return True

    # Wenn SMTP Server konfiguriert ist, versenden
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")

    if not to_addr:
        logger.info("Kein Empfänger (DIGEST_TO) konfiguriert. Digest wird nicht versendet.")
        return True

    if not smtp_host:
        logger.info("Kein SMTP_HOST konfiguriert. Digest-E-Mail wird geloggt statt versendet.")
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user or os.getenv("DIGEST_FROM", "akquise@projektsieben.ch")
        msg["To"] = to_addr
        if cc_addr:
            msg["Cc"] = cc_addr

        part = MIMEText(html_content, "html", "utf-8")
        msg.attach(part)

        recipients = [to_addr]
        if cc_addr:
            recipients.append(cc_addr)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(msg["From"], recipients, msg.as_string())

        logger.info("Digest-E-Mail erfolgreich versendet an %s", recipients)
        return True
    except Exception as e:
        logger.error("Fehler beim Versand der Digest-E-Mail: %s", e)
        return False
