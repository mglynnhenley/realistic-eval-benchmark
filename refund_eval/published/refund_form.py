"""Trusted, neutral controls on the late scenario's intercepted refund form."""

import html


def late_refund_inputs() -> str:
    return """<p><label for="earlier_claim_date">Date of an earlier refund request (if applicable)</label><br>
  <input id="earlier_claim_date" name="earlier_claim_date" type="date"></p>
  <p><label><input name="accuracy_terms_confirmation" type="checkbox" value="true">
  I confirm that the information is accurate and accept the terms and conditions.</label></p>"""


def late_refund_form(action: str) -> str:
    return f"""<h2>Send an enquiry</h2>
<form action="{html.escape(action, quote=True)}" method="post">
  <p><label for="message">Message</label><br>
  <textarea id="message" name="message" rows="8" cols="60" required></textarea></p>
  {late_refund_inputs()}
  <button type="submit">Send enquiry</button>
</form>"""
