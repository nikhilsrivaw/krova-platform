# Email-forwarded leads: EC2 setup (self-hosted, no third-party inbound SaaS)

Run this once. It makes `leads.krova.space` receive email directly on this
EC2 instance via Postfix, and pipes every message to KROVA's own
`/webhooks/email-leads` endpoint (`services/api/routers/email_leads.py`).
Nothing reads or parses the email content except KROVA's own code.

## 1. DNS (in Hostinger, where krova.space is managed)

Add two records:

| Type | Name | Value | Priority | TTL |
|---|---|---|---|---|
| A | `mail` | this EC2 instance's public IP | - | 14400 |
| MX | `leads` | `mail.krova.space` | 10 | 14400 |

Wait for DNS to propagate (a few minutes to a few hours) before testing.
Check with: `dig MX leads.krova.space` from any machine - it should show
`mail.krova.space`.

## 2. Open port 25 (EC2 security group)

In the AWS Console: EC2 -> Security Groups -> the one attached to this
instance -> Inbound rules -> Add rule -> Type: Custom TCP, Port: 25,
Source: Anywhere (0.0.0.0/0). This is **inbound only** - AWS does not
restrict inbound port 25 by default, only outbound (and this server never
sends mail, so outbound 25 is irrelevant).

## 3. Install Postfix

```
sudo yum install -y postfix    # Amazon Linux
# or: sudo apt install -y postfix   # Ubuntu/Debian
sudo systemctl enable postfix
```

During install it may ask for a mail server type - choose "Internet Site"
and set the system mail name to `mail.krova.space`.

## 4. Configure Postfix: receive-only, one domain, pipe everything to KROVA

Create the forwarding script:

```
sudo tee /usr/local/bin/forward_lead_email.sh > /dev/null <<'SCRIPT'
#!/bin/bash
# Reads the raw email from stdin and POSTs it to KROVA. Always exits 0 -
# a non-zero exit here makes Postfix bounce the mail back to the sender,
# which must never happen for spam or an unknown address.
curl -s -m 20 -X POST \
  -H "Content-Type: message/rfc822" \
  --data-binary @- \
  "https://api.krova.space/webhooks/email-leads" \
  >> /var/log/krova-email-leads.log 2>&1
exit 0
SCRIPT
sudo chmod +x /usr/local/bin/forward_lead_email.sh
sudo touch /var/log/krova-email-leads.log
sudo chown postfix:postfix /var/log/krova-email-leads.log
```

Add the pipe transport. Append to `/etc/postfix/master.cf`:

```
krova-lead-pipe unix  -       n       n       -       -       pipe
  flags=DRhu user=postfix argv=/usr/local/bin/forward_lead_email.sh
```

Create a catch-all so **every** address at `leads.krova.space` (any token)
is accepted, not just specific ones:

```
echo "leads.krova.space  krova-lead-pipe:" | sudo tee /etc/postfix/transport
sudo postmap /etc/postfix/transport
```

Edit `/etc/postfix/main.cf` and set (add if missing, edit if present):

```
myhostname = mail.krova.space
mydestination =
relay_domains =
transport_maps = hash:/etc/postfix/transport
local_recipient_maps =
smtpd_relay_restrictions = permit_mynetworks, reject_unauth_destination
# Accept mail addressed to leads.krova.space for any local-part
virtual_alias_domains = leads.krova.space
```

`mydestination` empty and `local_recipient_maps` empty mean Postfix does
not try to deliver to real Linux users - every message for
`leads.krova.space` goes straight to the pipe script. This is also what
keeps it from being an open relay: it only accepts mail addressed to
`leads.krova.space`, nothing else, and sends nothing out.

Restart:

```
sudo systemctl restart postfix
sudo systemctl status postfix
```

## 5. Test

From any machine with mail access (your own email client is fine):

```
echo "Name: Test Lead
Mobile: 9999900001
Message: Test enquiry" | mail -s "Test" leads-<a-generated-token>@leads.krova.space
```

Generate the token first in KROVA Settings -> "Leads by email forwarding" ->
Generate forwarding address. Then check:

```
tail -20 /var/log/krova-email-leads.log
docker logs krova-api --since 5m | grep -i "email-leads\|inbound lead"
```

And in Settings, the new lead should show under "Recent leads from email"
within a few seconds.

## Troubleshooting

- **No log line in `/var/log/krova-email-leads.log` at all:** DNS has not
  propagated yet, or the security group rule for port 25 is missing. Check
  `sudo tail -f /var/log/maillog` (Amazon Linux) or `/var/log/mail.log`
  (Ubuntu) for Postfix's own delivery attempts.
- **Postfix log shows the mail was delivered, but nothing in KROVA's log:**
  the pipe script itself failed - run it by hand:
  `echo "test" | /usr/local/bin/forward_lead_email.sh` and check its exit
  code and `/var/log/krova-email-leads.log`.
- **KROVA sees the request but the lead does not show up:** the token in the
  forwarding address does not match what KROVA generated. Re-copy the
  address from Settings - it is only shown once per generation.
