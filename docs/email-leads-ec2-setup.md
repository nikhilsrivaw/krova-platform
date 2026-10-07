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
# Owned by nobody, not postfix: the pipe below runs as nobody (Postfix
# refuses to run a pipe command as its own mail-system owner).
sudo chown nobody:nobody /var/log/krova-email-leads.log
```

Add the pipe transport. Append to `/etc/postfix/master.cf`:

```
krova-lead-pipe unix  -       n       n       -       -       pipe
  flags=DRhu user=nobody argv=/usr/local/bin/forward_lead_email.sh
```

`user=nobody`, not `user=postfix` - Postfix's pipe(8) agent refuses to run
the delivery command as its own mail-system owner (`postfix`), confirmed on
a real run (`fatal: user= command-line attribute specifies mail system
owner postfix`).

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
relay_domains = leads.krova.space
transport_maps = hash:/etc/postfix/transport
local_recipient_maps =
inet_interfaces = all
smtpd_relay_restrictions = permit_mynetworks, reject_unauth_destination
```

Three things confirmed the hard way on a real setup:

- **`inet_interfaces = all`** is required. A fresh Postfix install on Amazon
  Linux listens on `127.0.0.1:25` only (confirm with `sudo ss -tlnp | grep
  :25`) - mail from the outside never reaches it until this is set.
- **`relay_domains = leads.krova.space`**, not `virtual_alias_domains`. A
  `virtual_alias_domains` entry makes Postfix require every recipient to be
  listed in a `virtual_alias_maps` table, and rejects anything else as
  `User unknown in virtual alias table` - the opposite of the catch-all this
  needs. `relay_domains` accepts every recipient at the domain by default.
- **`mydestination` empty and `local_recipient_maps` empty** mean Postfix
  does not try to deliver to real Linux users - every message for
  `leads.krova.space` goes straight to the pipe script. This, together with
  only relaying the one domain above, is what keeps it from being an open
  relay: it accepts mail addressed to `leads.krova.space` only, and sends
  nothing out on its own.

Restart:

```
sudo systemctl restart postfix
sudo systemctl status postfix
```

## 5. Test

Generate a forwarding address first, in KROVA Settings -> "Leads by email
forwarding" -> Generate forwarding address.

**Local test first** (isolates Postfix + the pipe script + KROVA's endpoint
from anything DNS/internet-related - do this before testing from a real
inbox). `mail`/`mailx` is not installed by default on Amazon Linux, but
`sendmail` always is, via Postfix itself:

```
printf "To: leads-<token>@leads.krova.space\nFrom: test@localhost\nSubject: Test\n\nName: Test Lead\nMobile: 9999900001\nMessage: local test\n" | sendmail -t
```

**Then test from a real inbox** (Gmail, or whatever the business uses), same
address, same kind of body text.

After either test:

```
sudo tail -20 /var/log/krova-email-leads.log
docker logs krova-api --since 5m | grep -i "email-leads\|inbound lead"
```

And in Settings, the new lead should show under "Recent leads from email"
within a few seconds.

## Troubleshooting

- **No log line in `/var/log/krova-email-leads.log` at all, and the local
  `sendmail` test above also shows nothing in Postfix's own log:** check
  `sudo ss -tlnp | grep :25` - if it shows `127.0.0.1:25` instead of
  `0.0.0.0:25`, `inet_interfaces = all` is missing or wasn't applied; restart
  Postfix after setting it.
- **No log line, but DNS/Postfix both look right:** DNS may not have
  propagated yet from outside this VPC, or the security group rule for port
  25 is missing. Check Postfix's own log for delivery attempts - Amazon
  Linux 2023 has no `/var/log/maillog` by default (logs go to the systemd
  journal): `sudo journalctl -u postfix --since "10 minutes ago" --no-pager`.
  On Ubuntu, check `/var/log/mail.log` instead.
- **Postfix log shows `status=bounced (User unknown in virtual alias
  table)`:** `virtual_alias_domains` is set instead of `relay_domains` - see
  step 4 above.
- **Postfix log shows `fatal: user= command-line attribute specifies mail
  system owner postfix`:** the pipe's `user=` in `master.cf` is set to
  `postfix` instead of `nobody` - Postfix refuses to run a pipe as its own
  mail-system owner.
- **Postfix log shows `status=sent`, but the DSN text names a `Permission
  denied` on `/var/log/krova-email-leads.log`:** the log file is owned by
  someone other than `nobody` (the pipe runs as `nobody`). The `curl` call
  never ran in this case either, since bash skips the command when it can't
  open the redirect target - `sudo chown nobody:nobody
  /var/log/krova-email-leads.log` and resend.
- **Postfix log shows the mail was delivered, nothing failed, but still
  nothing in KROVA's log:** run the script by hand as a sanity check:
  `echo "test" | sudo -u nobody /usr/local/bin/forward_lead_email.sh` and
  check its exit code and `/var/log/krova-email-leads.log`.
- **KROVA sees the request but the lead does not show up:** the token in the
  forwarding address does not match what KROVA generated. Re-copy the
  address from Settings - it is only shown once per generation.
