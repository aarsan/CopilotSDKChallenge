# InfraForge Demo Video Script (~3 Minutes)

---

## SCENE 1 — The Problem (0:00–0:30)

**[SETTING: Living room or home office setup. DAUGHTER (8 years old) sits at a desk wearing a badge or lanyard, playing the role of an IT Manager. She's on a phone call (toy phone or real phone held up).]**

**DAUGHTER:**
*(in her best serious-boss voice)*
"Hi, this is the IT department. ... No. No, you can't have Azure for your project. ... Because the Terraform scripts aren't built yet. ... I don't know, maybe a few weeks? ... No, we can't go faster — Security hasn't vetted it. ... And the policies haven't been written either. ... Yes, I know your deadline is Friday. ... I'm sorry, there's nothing I can do. We'll put you in the queue."

*(She hangs up, sighs dramatically, and the phone immediately rings again.)*

**DAUGHTER:**
*(picks up, exhausted)*
"IT Department... yes, another Azure request? ... Get in line."

*(She drops her head on the desk.)*

**[TEXT ON SCREEN]:**
> "In most enterprises, getting a new Azure service approved and deployed takes weeks to months."

**[CUT TO: Screen recording or narration begins.]**

---

## SCENE 2 — Introduce InfraForge (0:30–0:55)

**NARRATOR (Ahmet — voiceover or on-camera):**
"This is the reality of cloud adoption in the enterprise. Every new Azure service has to go through governance reviews, Terraform or Bicep scripts have to be hand-written, security policies have to be authored, compliance has to sign off — and all of that requires expensive, specialized staff."

"What if you could replace that entire bottleneck with an AI-powered IT organization — one that handles governance, infrastructure-as-code, policy generation, and deployment automatically?"

"This is InfraForge. Built on the GitHub Copilot SDK, powered by Claude."

**[SHOW: InfraForge web UI — the dashboard / landing page.]**

---

## SCENE 3 — The Service Catalog & Governance (0:55–1:25)

**NARRATOR:**
"InfraForge starts with your Service Catalog. Every Azure service your organization uses is tracked here — its approval status, risk tier, security policies, and allowed configurations."

**[SHOW: Click into the Service Catalog. Browse a few services. Show one that is 'Not Approved' and one that is 'Approved'.]**

"When a team requests a new service that hasn't been onboarded yet — like Azure Cosmos DB or an App Service — that's where the magic happens."

**[SHOW: Click 'Onboard' on a service that isn't approved yet.]**

"Instead of waiting weeks for a platform engineer to write the Terraform, an architect to review it, and the security team to author policies — InfraForge handles the entire lifecycle automatically."

---

## SCENE 4 — Onboarding Pipeline In Action (1:25–2:10)

**NARRATOR:**
"Watch. With one click, InfraForge kicks off a fully automated onboarding pipeline."

**[SHOW: The onboarding pipeline running with the NDJSON flowchart — steps completing in real time:]**

"First, it checks dependency gates — does this service require other services to be onboarded first?"

"Then it analyzes your organization's security standards and generates a deployment plan."

**[SHOW: Steps ticking by — ARM template generation, Policy generation.]**

"It generates a production-ready ARM template and an Azure Policy definition — not from a static template, but intelligently, based on your organization's specific standards."

"Next, it runs a full governance review. Our virtual CISO evaluates the security posture, risk level, and compliance alignment."

**[SHOW: Governance review step completing.]**

"Then it actually deploys the template to Azure to validate it works. If the deployment fails — and in the real world, it often does — InfraForge self-heals. It reads the error, feeds it back to Claude, gets a fix, and retries. Up to five times."

**[SHOW: Validation / healing loop if visible, or the green checkmarks on completed steps.]**

"It even generates infrastructure tests and runs them against the live deployment to make sure everything is functional."

"Then it cleans up the test environment, promotes the service to 'Approved,' and it's ready for any team in the organization to use."

---

## SCENE 5 — The Chat Interface & Composition (2:10–2:35)

**NARRATOR:**
"But InfraForge isn't just pipelines. Teams can also talk to it."

**[SHOW: Open the Infrastructure Designer chat. Type something like: "I need a web app with a SQL database and a Redis cache behind a VNet."]**

"Through natural language, any developer can describe what they need. InfraForge checks governance, searches the template catalog for pre-approved building blocks, composes them together, estimates cost, validates compliance, and can deploy — all from a conversation."

**[SHOW: The chat responding — searching catalog, composing, showing results.]**

"No Terraform expertise required. No tickets. No waiting."

---

## SCENE 6 — The Punchline (2:35–3:00)

**[CUT BACK TO: DAUGHTER at the same desk — but the "IT Manager" act is over. She's completely absorbed in a kid activity: coloring a picture / making a beaded bracelet / building with LEGOs / playing with slime (pick whichever is funniest on camera). Badge still hanging around her neck.]**

**[Phone rings. She barely glances at it, picks it up with one hand while still focused on her activity.]**

**DAUGHTER:**
*(barely paying attention, not even looking up)*
"Yeah? ... Mm-hmm ... You need Kubernetes? ... Hold on."

*(Without putting down her crayon / bracelet / slime, she leans toward the laptop, still not really looking, and casually says into the mic:)*

**DAUGHTER:**
"Hey InfraForge — onboard AKS in Central US."

*(She immediately turns back to her activity, completely unbothered. Beat. The phone is still to her ear.)*

**DAUGHTER:**
*(glances at the screen for half a second)*
"Yeah, it's done. You're welcome. Bye."

*(She hangs up without ceremony and goes right back to coloring / crafting. Doesn't even look up.)*

**[SHOW: Quick cut to the InfraForge screen — the onboarding pipeline completing with green checkmarks, service promoted to "Approved."]**

**[TEXT ON SCREEN]:**
> **InfraForge**
> So easy, your 8-year-old could run your platform team.
>
> AI-Powered Cloud Adoption, Built on the GitHub Copilot SDK.

**NARRATOR (voiceover):**
"InfraForge. Stop staffing bottlenecks. Start shipping infrastructure."

**[END]**

---

## Production Notes

- **Total runtime target:** ~3:00
- **Screen recordings needed:** Dashboard, Service Catalog, Onboarding Pipeline (running), Chat Interface (composing)
- **Daughter's scenes:** Can be filmed separately and spliced in — two short scenes (~15 sec each)
- **Key talking points to hit:**
  - Governance, IaC, pipelines, and deployment are ALL automated
  - Built on GitHub Copilot SDK + Claude
  - Self-healing deployment pipeline
  - Natural language interface — no Terraform/Bicep expertise needed
  - Cost of staffing these roles vs. automation
  - Weeks-to-months reduced to minutes
