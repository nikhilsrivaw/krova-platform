# Team and roles

## Adding people
Team page → Add a team member. Enter a name, a Team ID and a role. You get a Team ID and password to hand over (you can send it on WhatsApp); the password is shown only once. They sign in on the team sign-in page with that Team ID and password, then set their own password.
Your plan sets how many team logins you can have.

## Roles
- Agent: replies, approvals, escalations and customers (CRM).
- Admin: everything an agent does, plus campaigns, automations and settings.
- Owner: everything, plus billing and plan, adding admins and changing roles. Ownership can be transferred to another person (you become an admin).

## Managing people
Reset password, mark Away (gets no new round-robin chats), make owner, remove. A locked account needs a password reset.

## How chats are shared
- The first agent to reply owns the chat; others must take it over to answer (can be turned off).
- New chats: Round-robin (the default - the available agent who got one longest ago, agents only, never someone marked Away) or "I assign them". Round-robin covers every chat that no team rule catches, so a business with no teams still shares chats across all its agents. With Round-robin, a new lead from Justdial, IndiaMART, a portal or a form also goes to the next agent the moment it arrives, with a "New lead for you" notification. A customer who already belongs to someone stays with them. Without Round-robin, a lead has no owner until someone replies or an automation assigns it ("A new lead comes in" → Assign the chat).
- Reply-time target: if a customer waits this long, the chat's owner is pinged (a chat nobody has is first given to a person); at double, admins too.
- AI first, a person when needed: when the AI replies on its own (Autonomy set to Act or Conditional, and always on calls and the website chat), a new chat is not handed to anyone straight away - the AI answers. It goes to a person (its team's way, or round-robin) only when the AI asks for help or the customer asks for a person, when an AI reply is waiting for someone's OK, or when the customer has waited past the reply-time target. With Autonomy on Draft or Observe every reply needs a person, so the chat is given to someone at once.
- What agents can see: every chat, or only their own and unowned chats. This applies everywhere a customer shows up - Inbox, Customers, Ledger, Approvals, Escalations, Leads and payment links - not only the Inbox. Owners and admins always see every chat. Private chats are never shown to agents.

## Teams and routing (complaints to one team, leads to another) (not on the Solo plan)
Team page → Teams and routing (owner and admins).
- Teams: make teams such as Complaints, Sales, Accounts or a branch. Pick the members (a person can be in several), an optional team lead, the team's own reply-time target (or use the business one), and how chats are shared inside it: Round-robin (straight to the next member who is not Away) or Team queue (it waits in the team, everyone in it is told, whoever replies first takes it).
- Routing rules: "When a conversation ... send it to team ...". A rule can check words in the message (Hindi or English, comma separated), the language the customer writes in (English, Hinglish, Hindi/Marathi, Tamil, Telugu, Bengali, Gujarati, Kannada, Malayalam, Punjabi, Odia - for example English speakers to a team whose people write good English), the channel (WhatsApp, Instagram, calls, website chat), which of your numbers it came to, which lead source a lead came from (Justdial, IndiaMART, portals, forms), and a tag on the customer. Leave a box empty to not check it; a rule with nothing filled catches everything. Rules are checked top to bottom and the first match wins - use the arrows to reorder.
- Suggested teams: some types of business get a ready set. A school, college or university sees Exams & Results, Fees & Accounts, Student services and Admissions, each with the words that send a question to it (result, admit card; refund, receipt; hostel, scholarship, TC; admission, course, fee). "Make these teams" creates the ones you do not have yet, empty and with their rules switched off - add people to each team, then open its rule and switch it on. Change the words any time.
- "Start with a complaints rule" fills a rule with common complaint words (complaint, shikayat, refund, kharab, not received...). Edit the words for your business.
- "Move it even if someone already has the chat": for complaints, so a complaint from a customer who belongs to a sales person still reaches the Complaints team; that person is told.
- "Give it to a person straight away": the chat goes to a team member the moment it matches, even while the AI answers on its own. The complaints starter rule has it on.
- Rules run on a new message in a chat nobody owns (or with that switch on, any chat), and on every new lead. A chat already in the rule's team is not moved again. If nobody in the team is available, the chat waits in the team and the team lead is told. A chat that matches no rule is handled as set above (round-robin, or you assign it). When the AI goes first, a matching chat joins the team and a team member gets it when a person is needed.
- Test a message: type a sample message to see which rule would catch it and which language it reads as. Nothing is sent or moved.
- Language: Indian scripts are read exactly. English and Hinglish (Hindi typed in English letters) are told apart by common Hindi words, so a very short message such as "ok thanks" counts as English. On calls, the language heard on the call is used. The language is judged when the conversation is routed - a chat already in a team is not moved because the customer switched language.
- Late replies: when a team chat passes the team's reply target, the person who has it and the team lead are pinged; at double, the owner and admins too.
- In the phone app, the chat shows its team, and the Inbox has a "My team" filter for people in a team.

## What agents can also do (Team page, owner and admins set it)
- See business money - off by default. When off, agents don't see what you are owed in total, overdue totals, deal values or call costs. Each customer's own amount due stays visible so they can collect it.
- Take payments - on by default. Record a payment and send a payment link.
- Send templates - on by default. Reach a customer with an approved template after the 24-hour window has closed.
- Customer phone numbers - Shown (default) or Last 4 digits only. With last 4 digits, agents can still message the customer and call them through KROVA (their phone rings first), but can't see or copy the full number.
Owners and admins can always do all of this.

## Talk to team (customers who asked for a person)
Sidebar → Talk to team (everyone on the team; phone app: More → Talk to team, and on Today).
- When a customer asks to talk to a person or to be called back - on a call, WhatsApp, Instagram or the website chat ("team se baat karni hai", "manager ko bulao", "call me back"), in English letters or in Hindi script ("मुझे टीम से बात करनी है", "manager से connect करो"), or any other way of asking for a real person - it lands here. On a call, KROVA first tries to put them through to your team; if nobody can take the call, it lands here. A missed call that nobody on the call list picked up also lands here ("Missed call - call back").
- This is not an escalation: Escalations are questions the AI could not answer. Talk to team is a customer who wants a person.
- In a chat the customer is told their ask reached the team and asked if a particular time suits them. On the website chat a visitor is first asked for their name and number - without one nobody could reply.
- Who hears: the chat's team (Teams and routing) when it has one, otherwise everyone who is available, plus the owner and admins. If nobody takes it in 15 minutes, the owner and admins are reminded once.
- Waiting / Mine / Done tabs. A customer who asks again while their request is open is added to the same request, not a new one.
- Open a request for the side panel: what they want, the customer's briefing (every channel's recent messages, notes, promises, money owed, next visit), and buttons - "I'll take it" (the request and the customer's chat become yours), Call now (your phone rings first, then the customer), Open chat, Put back. Owner and admins can also take one over or "Give it to" a named person, who is told.
- Done: write what happened. The note is saved on the customer, so the whole team sees what was agreed. "Close without talking" closes it with no call.

## Activity feed
Team page → Activity feed: who did what and when - messages sent, replies approved, settings changed, data downloaded, sign-ins, and refused attempts. It never shows what a message said. Rows can't be edited or deleted.
