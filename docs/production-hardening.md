# Auto Browser Production Hardening Spec

## Goal

Ship Auto Browser as a safe **single-tenant private beta** first, then harden toward a broader internal production tool.

## Hard requirements for the private beta target

- Startup refuses to boot without `API_BEARER_TOKEN` (at least 32 characters)
  whenever the API is reachable off-box — that is, `API_BIND_SCOPE=exposed`,
  which is the default for any deployment that does not declare otherwise. The
  token may also be placed in `API_TOKEN_FILE`. The shipped base compose declares
  `loopback` to match its `127.0.0.1` publish mapping; with no `API_BEARER_TOKEN`
  the controller then generates a token in `API_TOKEN_FILE` (`./data/api-token`)
  and demands it, because every container on the compose network can reach port
  8000. `API_ALLOW_TOKENLESS_LOOPBACK=true` serves loopback without a token; it
  never applies to an exposed or production controller.
- Production startup additionally refuses to boot without:
  - `API_BEARER_TOKEN`
  - `REQUIRE_OPERATOR_ID=true`
  - `AUTH_STATE_ENCRYPTION_KEY`
  - `REQUIRE_AUTH_STATE_ENCRYPTION=true`
  - `CONTROLLER_ALLOWED_HOSTS` configured for the controller ingress hostnames
  - `ALLOWED_HOSTS` naming the sites the browser's top-level page may visit (not `*`); iframes and subresources from other hosts are not filtered
  - `SHARE_TOKEN_SECRET`, so share links survive a restart
  - request rate limiting enabled
- Keep `PRE_REQUEST_BLOCKING=true` (the default), so a navigation to a host off `ALLOWED_HOSTS` is stopped before
  it is sent rather than refused after it loads. With `CDP_CONNECT_URL` or `browser.cdp_attach`, start that Chrome
  with `--disable-features=BlockOriginHeaderModificationOnRedirect,AvoidCorsURLLoaderRestartOnRedirect` and set
  `CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED=true`; without it, cross-origin 307/308 POST redirects can fail, and on
  Chrome 155 and later credentialed, preflighted cross-origin requests fail after a redirect, in the operator's
  own tabs too, while the gate is attached. What is stopped and what is not:
  [`docs/deployment.md`](./deployment.md#stopped-before-the-request-is-sent). On Docker Desktop, browser
  containers can reach that Chrome's remote-debugging port: keep it off a host whose browser containers load
  pages you don't trust ([`docs/deployment.md`](./deployment.md#per-session-networks-docker_ephemeral)).
- Operator identity is only authentication when it comes from a named
  credential (`API_BEARER_TOKENS=alice:token-a,bob:token-b`), which records
  `source: "token"` on the audit event. With the shared `API_BEARER_TOKEN`,
  `X-Operator-Id` is attribution only and is recorded as `source: "header"`.
  When a header disagrees with the authenticated operator, the credential wins
  and the claim is kept in `asserted_id`.
- Under named credentials, sessions and auth profiles belong to the operator
  who created them. Another operator's session is left out of listings and
  answers like a session that does not exist, together with its approvals,
  agent jobs, cron jobs and `/artifacts` files; nothing under `/artifacts` that
  is not inside a session directory with a stored record is served to anyone.
  Agent jobs run as the operator who queued them, and cron jobs fire as their creator, opening only the auth
  profiles their creator may open. Neither outlives that operator's named credential: a cron fire is refused and a
  queued job fails once `API_BEARER_TOKENS` no longer names them, as after a move to the shared token.
  Sessions created under the shared token are
  unowned and open to every operator. The audit log stays operator-wide (an
  approval's id, kind, origin and tool, and its decision's comment, included), and
  the takeover desktop is shared by every session on a browser node; use
  `docker_ephemeral` isolation to give each session its own.
- Some surfaces belong to no operator, so every credential can use them: the maintenance sweep and its status
  (`POST /maintenance/cleanup`, `GET /maintenance/status`), the readiness report (`GET /readiness`,
  `browser.readiness_check`), the proxy personas (the `/proxy-personas` routes and the `browser.list_proxy_personas`,
  `browser.create_proxy_persona` and `browser.delete_proxy_persona` tools) and the mesh peer list
  (`GET /mesh/peers`). `API_ADMIN_OPERATORS=alice,ops` leaves them to the operators it lists, each proven by a named
  credential. Anyone else is answered `403` with a fixed text over REST and a tool error (`code: not_permitted`) over
  MCP, and nothing is read or changed. Unset, every credential may use them. Once it is set, the shared
  `API_BEARER_TOKEN` is refused there, whatever its `X-Operator-Id` header says, because it proves no operator: a
  script, a dashboard tab (its Mesh Peers table) or an MCP client that uses these surfaces needs a listed operator's
  named token. Ids are compared exactly, case included, and an id that no credential in `API_BEARER_TOKENS` proves
  admits nobody. Among the listed operators a proxy persona still belongs to the one who saved it, and opening a
  session with a persona is not one of these surfaces.
- Each approval decision records `decided_by`, the operator a named credential proved (for an agent's decision, the
  operator it acts for), in the record, `GET /approvals`, the decision's audit event and its witness receipt's
  `metadata`; it is `null` without named credentials. `APPROVAL_REFUSE_SELF_APPROVAL_OPERATORS=alice,ci-bot`
  refuses a listed operator an approve of an approval they asked for (`409`), the withdraw of a rejection waiting on
  it included; their rejections, and an agent's approval under `AUTONOMOUS_APPROVALS`, are not affected. Under named
  credentials only the operator who asked may use an approval in a session they own or in controller scope, so a
  listed operator's approvals there stay pending: list operators whose actions run in sessions created under the
  shared token, which every operator may use.
- `browser.cdp_attach` is refused under `BROWSER_SANDBOX=on`: the controller cannot verify the sandbox
  of a Chrome reached over CDP (over `POST /mcp/tools/call`, as every tool refusal, that is HTTP 200 with
  `isError`). `CDP_CONNECT_URL` is the operator's own way to use such a Chrome.
- `browser.cdp_attach` swaps the one browser every session runs on, so it is refused
  whenever more than one credential is configured (named credentials, or a named one
  next to the shared token): the attached browser would be shared by every operator.
  These two refusals (under `BROWSER_SANDBOX=on`, above, and with more than one
  credential) are the tool's guard. The attach URL is the caller's to name, as
  `CDP_CONNECT_URL` is the operator's, so it is not checked against `ALLOWED_HOSTS`.
  Run a separate controller per operator, or set `CDP_CONNECT_URL` at startup. With a
  single credential it is refused while live sessions still use the current browser,
  and the browser it replaces is disconnected.
- Governed approvals are the calling agent's choice unless the server makes them its own:
  `workflow_profile` is an argument the agent supplies and defaults to `fast`, so on a
  multi-operator deployment an agent that leaves it out can run ordinary writes without
  governed approval; the runtime approvals still apply (an upload's, with
  `REQUIRE_APPROVAL_FOR_UPLOADS` on by default, and those of `post`, `payment`,
  `account_change` and `destructive` decisions), and `browser.eval_js` always asks: an operator,
  unless `AUTONOMOUS_APPROVALS=true` lets the agent approve its own (below).
  `FORCE_GOVERNED_APPROVALS=true` takes the choice away. It is off by default because it
  stalls an existing agent loop on its first write until an operator approves. Readiness
  (`GET /readiness`, `browser.readiness_check`) warns `governed_approvals` when more than
  one operator credential is configured and it is off. What it covers and what it does not:
  - It covers every MCP tool call that has a governed kind (every cron tool but
    `browser.list_cron_jobs`,
    `browser.execute_action`, `browser.set_cookies` and `browser.delete_memory_profile` among
    them) and every step of the agent loop: MCP-queued steps and runs (`browser.queue_agent_step`,
    `browser.queue_agent_run`), REST `/agent/step` and `/agent/run`, and cron runs. Each runs as
    `governed` whatever `workflow_profile` the request carried, so an operator approves each write.
  - It does not cover the REST action routes (`/sessions/{id}/actions/*`). They take no
    `workflow_profile` and ask for approval only for uploads and for `post`, `payment`,
    `account_change` and `destructive` decisions, as before. An agent holding a bearer token can
    call them directly. The setting is a control on agents that act through MCP tools and the
    agent loop; it is not a sandbox for a token holder.
  - `AUTONOMOUS_APPROVALS=true` lets the agent approve its own pending actions
    (`browser.approve_approval`, and the agent loop itself), so with it on the forcing only adds
    a step the agent performs itself. Keep it off wherever `FORCE_GOVERNED_APPROVALS` is meant
    to be a control.
  - An approval of a tool that names no session (`browser.delete_memory_profile`,
    `browser.cdp_attach`) is held in a controller-wide scope and belongs to the operator who
    asked, as described in `docs/mcp-clients.md`.
- In the fast profile a click's `risk_category` is the model's word, and text on a page can steer the model, so a
  click labelled `read`, `write` or `upload` (an unlabelled click is `write`; only the upload action asks for
  `upload`) has its target read first, found as the click will find it. So has a `type`: it puts the caret in its
  target by a press on it, and aimed at a button it presses the button. This covers what carries a label: an agent
  step, `browser.execute_action` and `POST /sessions/{id}/actions/execute`. The direct routes
  (`POST /sessions/{id}/actions/click` and `/actions/type`) carry none and ask for nothing, as before. An
  `element_id` or a point is read in the controller's isolated world, a point through open shadow roots and into
  frames of the page's own origin (an `iframe`, a `frame`, or a document that an `object` or an `embed` holds;
  three deep). An `embed` is entered where it is among the first 256 frames of its page; one that comes later
  is read as the element, which is no button. Looking through that many takes time: behind 255 frames, the read
  at the press of an embed of the page's own took 0.35 seconds on an idle machine and ran past its half second on
  a loaded one, and the press then went on unread (the read at the press, below). A selector is resolved by
  Playwright, as the click resolves it (CSS through open shadow roots, `text=`, `role=`, `xpath=`, `>>` chains),
  and its element is described where the page's script runs.
  - A button (`button`, an `input` of type `button`, `submit`, `image` or `reset`, `role=button`, or
    `role=menuitem`, which is a menu's command) asks for a kind's approval, as an honest label does, when one of
    its names starts, as whole words, with a phrase of that kind in `controller/app/browser/click_target.py`. An
    element inside a button counts as the button, and so do a `label` for or around one, a link inside a button or
    a menu item (a menu whose items carry a URL keeps each item's text in a link), text a page slots into a
    component's button, and a component whose shadow tree puts all it is given into one button. A button's names
    are its `aria-label`, its visible text (with what a slot or a shadow root inside it shows), its `title`, an
    input's value or alt text, the text of up to four `label` elements for it, an `aria-label`, `title` or alt
    text on up to four elements inside it (an icon's name), and the first four `title` and `text` elements of an
    SVG in it (where the button is itself an SVG, that is the text it shows). The table:
    - `payment`: `Pay`, `Buy`, `Purchase`, `Place order`, `Place your order`, `Order now`, `Confirm order`,
      `Confirm payment`, `Confirm purchase`, `Confirm and pay`, `Complete order`, `Complete purchase`,
      `Submit order`, `Submit payment`, `Donate`, `Transfer`
    - `destructive`: `Delete`, `Remove`, `Erase`, `Permanently delete`, `Yes, delete`, `Yes, remove`,
      `Confirm delete`, `Confirm deletion`, `Empty trash`, `Cancel subscription`, `Close account`, `Deactivate`
    - `account_change`: `Change password`, `Update password`, `Reset password`, `Change email`, `Change e-mail`,
      `Update email`, `Update e-mail`
    - `post`: `Publish`, `Post`, `Send`
    - Not asked about, though each starts with a phrase above: `Remove filter`, `Remove filters`,
      `Remove all filters`, `Remove from cart`, `Remove from basket`, `Remove from bag`, `Remove from compare`,
      `Remove from wishlist`, `Remove from favorites`, `Remove from favourites`, `Remove from playlist`,
      `Remove from queue`, `Remove from watchlist`, `Remove from library`, `Remove from list`,
      `Purchase history`, `Purchase order`, `Purchase orders`, `Pay as you go`, `Post code`. One that goes
      on, on the same line, to a phrase of the lists above asks all the same ("Remove from library and delete
      files").
    - Nor is `Send` asked about when it sends a way to sign in ("Send code", "Send me a login link", "Send reset
      link to my email"): the word itself, then nothing or words of the next list, then one of: `code`, `otp`,
      `passcode`, `one-time password`, `one time password`, `one-time pin`, `one time pin`,
      `verification email`, `confirmation email`, `activation email`, `recovery email`, `reset email`,
      `login email`, `sign-in email`, `reset instructions`
    - Between the two only these words may stand: `me`, `us`, `a`, `an`, `the`, `my`, `new`, `another`, `login`,
      `log-in`, `log in`, `sign-in`, `sign in`, `signin`, `magic`, `reset`, `password`, `verification`,
      `confirmation`, `activation`, `security`, `one-time`, `one time`, `single-use`, `recovery`,
      `authentication`, `2fa`, `mfa`, `sms`, `email`, `text`
    - A `link` is a way to sign in only where one of these words stands before it: `me`, `us`, `login`, `log-in`,
      `log in`, `sign-in`, `sign in`, `signin`, `magic`, `reset`, `password`, `verification`, `confirmation`,
      `activation`, `one-time`, `one time`, `single-use`, `recovery`, `authentication`. "Send link" by itself
      is also the button of a share dialog, which sends a document to whomever its form names, so it asks, as
      "Send a link", "Send link by email" and "Send link to everyone" do.
    - After it the label ends, or goes on with one of: `to`, `via`, `by`, `through`, `using`, `in`, `on`, `again`,
      `now`, `instead`. So "Send payment link", "Send gift code" and "Send code review request" ask, and so
      do "Send email", "Send SMS", "Send again" and "Send PIN" (a pin is also what a map or a board sends).
    - From such a word on the label may say only where the code goes or what it is for: `me`, `my`, `your`, `the`,
      `this`, `a`, `an`, `and`, `or`, `at`, `with`, `email`, `e-mail`, `mail`, `inbox`, `address`, `phone`,
      `mobile`, `cell`, `number`, `device`, `sms`, `text`, `message`, `call`, `voice`, `app`, `whatsapp`,
      `authenticator`, `registered`, `primary`, `backup`, `recovery`, `work`, `personal`, `other`, `another`,
      `different`, `new`, `ending`, `reset`, `password`, `verify`, `confirm`, `sign`, `log`, `login`, `continue`,
      `account`, `identity`, `you`, `it's`, `it’s`, `into`, `proceed`, `access`, `trusted`, `saved`, `same`,
      `file`, `browser`, `computer`, `we`, `have`, `of`, `for`, `associated`, `linked`, `above`, `below`, `last`,
      `digits`, `landline`, `telephone`, `notification`, `push`, `seconds`, `second`, `minutes`, `minute`, `up`,
      `sign-in`, `sign-up`, `log-in`, `signin`, `signup`, `complete`, `create`, `register`, `get`, `started`,
      `automated`, `telegram`, `google`, `microsoft`, `registration`, `2fa`, `mfa`, `alternate`, `alternative`.
      Two such words may be joined by a slash ("SMS/Email"). One of the words a label goes on with (the bullet
      before) may stand there too, and an address as a page shows the user's own: a name with an at sign after it,
      which may be masked from its first character where a domain with a dot follows ("j***@example.com",
      "***@gmail.com"), or a number as a page masks one, made of digits, asterisks, bullets, the letter x and a
      phone number's marks and nothing else, each group of it with a digit, an asterisk, a bullet or two x in it
      ("+1 (***) ***-1234", "(XXX) XXX-1234"); a count of seconds ("30s") too. A mark that is no letter and no
      digit and stands by itself (an arrow, a tick) says nothing, here and anywhere in a label that Send opens
      ("Send code →" runs; "Send code → John" asks). Written onto the word for what is sent, a mark makes it
      another word, and the label asks: "Send code…" and "Send code." do, where "Send code …" and "Send code
      via SMS." run. A word behind an at sign names somebody, whatever the word and whatever mark stands before
      the sign ("@backup", "@1234", "-@backup"), except where the mark is an asterisk or a bullet and the word
      has a dot in it, which reads as a masked address ("*@backup.io" runs). A word behind a hash names a
      channel unless it is such a number ("#work" asks, "phone #1234" runs). So "Send code to my phone" and
      "Send code to reset your password" run, and "Send code to all users", "Send verification email to John",
      "Send code to team2", "Send code to #work" and "Send code to verify payment" ask. An address written out
      in full ("Send code to pat@example.com") runs, whoever's it is.
    - A line of the label that stops on one of these words is not its end: `to`, `via`, `by`, `through`, `using`,
      `in`, `on`, `the`, `a`, `an`, `my`, `your`, `this`, `and`, `or`, `at`, `with`, `of`, `for`, `into`. The line
      after it is read with it, whatever that line opens with, so "Send code to" over "all users in the
      organization today" asks, and "Send code to" over "my phone number ending in 1234" runs. The line after
      that is read with the two only where they go on into it in the same way, so the small print under an address
      is no part of where the code goes: "Send code to" over "j***@example.com" over "Standard message rates may
      apply" runs. "Send code again" over a sentence about it runs: a label ends on "again", "now" or "instead".
  - A label is read as its whole text on one line, and then each of its first three lines by itself, so a name
    over a sentence ("Delete account" above "This permanently removes your profile") is read by its name. A line
    of over 40 characters is a sentence, not a name, and is not matched, and the page hands over the first 400
    characters of each name. A line the table matches is let go when the label from that line on is one of the
    harmless continuations, however the page breaks it ("Remove" over "filter"), and when the line by itself is
    one ("Send code" over a sentence about it), unless it is one of Send's and the next line opens with a word a
    label goes on with, or the line itself stops on a word that leaves it open (the last list above): "Send code"
    over "to all users in the organization" asks, and the same label on one line, which is past 40 characters, is
    not read. Case and spacing do not count; characters that show nothing (zero-width ones, soft hyphens, the
    marks that set a text's direction) are removed; up to two leading tokens that are a number, a mark or an icon
    font's ligature name are skipped ("1 Delete", "credit_card Pay now"), and a label that is one ligature name is
    read with its underscores as spaces ("delete_forever"; one with a dot is a file's name, "delete_me.txt"); a
    hyphen goes on with its word ("Pay-as-you-go" is not "Pay"; a dash does not, so "Pay – as you go" asks); the
    comma of `Yes, delete` may be missing.
  - An icon font's name of one word stands on the label's line as a word in lower case ("payment Pay now",
    "warning Delete account"). So where one or two such words lead up to a capitalised one, the line is read
    from that one too, and asks when either reading does: "delete Remove filter" and "send Send code" ask,
    since "delete Purchase history" is a label as well.
  - A button often has several names (what it shows, its `aria-label`, its title). One the table matches is let
    go when another goes on from the same words to a harmless continuation: a button that shows "Send" and is
    named "Send code" for a screen reader, a filter chip that shows "Remove" and is named "Remove filter:
    Price".
  - Left out on purpose: bare `Close`, `Cancel`, `Dismiss`, `OK`, `Continue`, `Next` and `Accept` (they are
    everywhere and mostly harmless), `Checkout` and `Proceed to checkout` (they open a page), `Subscribe`,
    `Unsubscribe`, `Submit`, `Confirm`, `Save changes` and `Clear`.
  - A form's submit button with no such label, labelled `read`, is recorded as `write`, which asks for nothing, so
    search boxes and login forms keep working unattended: `Search`, `Sign in`, `Continue` and `Send code` run. A
    form whose button says `Reset password` asks, as the table says. One labelled `write` is untouched, and one
    found only at the press (the click named a wrapper around it) keeps `read` in its record.
  - An approval asked for this way says so: its reason opens with the controller's sentence, which names the kind
    the target reads as and the label the agent gave the action, and the agent's own reason follows, named as the
    agent's. Asked at the press (below), the sentence says "what this click would press", since the action names
    something else. Nothing read from the page is in the controller's sentence; the agent's reason may repeat what
    the page said. An agent can write the same sentence itself, and an action it labels with a kind of the table
    asks for that kind's approval with its target unread (below), so an approval does not show who chose its
    kind: judge it by the action and the target it names. An approval already pending for the same action and
    kind is reused as it stands, with the reason it was first asked with.
  - The press is read once more. The read above describes the element the action names, as the page stood before
    the approval decision; the press lands on what is under the pointer when the mouse goes down, and that can be
    another element: the button that fills a `div` the click named, or a row's `Delete` that the page shows only
    while the pointer is over the row. So with the pointer in place and the mouse still up, the element under it
    is read in the isolated world, and a button of a kind the action has no approval for is not pressed: the
    action answers `approval_required` of that kind, as above. Where the element is not what the pointer finds at
    the middle of its box (a wrapper wider than the viewport, a link broken over two lines, something over its
    middle), the click is one Playwright places, at a point it picks itself. For an action that is read,
    Playwright then only puts the pointer, at that point, which the controller gives it, and the press is the
    controller's own, read where it lands as any other. With the pointer placed, the target is asked once more
    that the pointer finds it there, and so is each frame it is in (the seven nearest the target; one further up
    is Playwright's alone to check): a target the page has taken out of its layout by then (`hidden`,
    `display: none`), made invisible, or put something over is not pressed (`target_moved`, retryable; one that
    has left the page, or was made invisible before the pointer was placed, is waited for, as a click waits, and
    ends the click when its time does; a page that does not answer the question within a second ends the click
    as a timeout, as at any step before it), and neither is one with no box of its own, whose children are laid
    out in its place (`display: contents`): where a press on it would land cannot be read, so the action answers
    `target_has_no_box`, and the agent names an element inside it. A `type`, whose press only puts the caret,
    focuses its field with no press wherever that press ends as `target_moved`, here and below, as where
    Playwright refuses its own click; a field that has left the page is then waited for once more, up to
    `ACTION_TIMEOUT_MS`. A target inside a frame is placed on what the screen shows of it, also where its frame
    runs off the screen or CSS draws the frame at another size.
    An element that CSS turns is placed at the middle of what the screen shows of the box around it (around the
    first of its boxes that the screen shows, where it has several), where Playwright takes the mean of that box's
    corners, each held to the screen. The two are one point while the turn is flat and none of the box is off the
    screen, unless the first box is thin: the controller measures a box by the rectangle round it and Playwright
    by its outline, and each skips a first box of under a square pixel by its own measure. Turned, even by 2
    degrees, a first box with no width (an empty inline block, a zero-width space before a block) has a rectangle
    of more than that and no outline, so the controller places the click on it, where there is nothing to press,
    and the click ends when its time does, where Playwright's own click presses the next box; one a sixteenth of
    a pixel wide and twelve high is pressed there, or the click ends so where something covers it. And turned or
    not, a first box 0.12 px wide and 12 high in a link whose font is 1 px high has a rectangle round it of a
    tenth of a square pixel (one line of the font) and an outline of 1.3: Playwright presses that box, and the
    controller the next box of the same link. Either way the click presses the element it names, or nothing.
    Turned in three dimensions with perspective they are apart (about 17 px on a button turned 50 degrees about
    two axes), and wherever Playwright's own click pressed such a button, in the turns measured, the controller's
    did. Where a corner of the screen cuts an element and its middle is off the screen, the controller's point
    can be beside the element (a square turned 45 degrees is so), and the click ends when its time does, where
    Playwright's own click presses. A link over two lines inside something CSS turns or skews is placed where
    Playwright presses it; inside a frame as well, it can be placed beside the link, and ends the same way.
    Playwright does not look through a frame that CSS transforms, or that stands inside anything
    CSS transforms (a dialog the page centres with a transform is one), so there the question above is the only
    one asked: where the page has put something of its own over such a frame, or the frame holds another frame
    that lies over the target, the click ends as `target_moved` with nothing pressed. A click that has to be
    placed on an element inside a frame asks the element's document where it saw the pointer arrive, and asks the
    element there: the browser gives a document the pointer's point through every border, padding and turn of
    the frames around it, so a frame of another process that is turned, bordered or padded is handled exactly
    while it stays so. That point is the pointer's only while the frame stays as it was: where the element's box
    has moved or changed its size since the click was placed (a page that moves the frame once the pointer has
    arrived, or scales one that runs in the page's process), the click ends as `target_moved` with nothing
    pressed. So does a scroll of the element's own document then, even where the element is still under the
    pointer (a card 60 px high scrolled 20 px). The box is Playwright's, which in a frame of another process is
    counted from the corner of the box round the frame as if the frame were not turned or scaled and had no
    border or padding. So a page whose own
    script, or a style in motion, turns or reshapes the frame as the pointer arrives and leaves that box as it
    was (a frame of another process given a half turn, or scaled about its top-left corner, or its padding grown
    inside the same border box) is a page that changes what is under the pointer, which is not caught (below):
    the click presses what the change put under the pointer. Where the document saw nothing, the pointer
    is over another document (or a listener of the page's on the window stopped both the pointermove and the
    mousemove before the controller's heard them), and the click ends as `target_moved`, unless a frame inside
    the element (a video in a card) lies at the point carried into the frame by the element's two boxes: that is
    taken for the element, as Playwright's own click takes it. A frame inside another frame is asked at such a
    point, carried into the outer frame's document, which holds where the outer frame is moved or scaled, and
    not where it is turned or mirrored or, as below, where it runs in a process of its own and has a border or
    padding; and a document that runs no script (a frame sandboxed without `allow-scripts`) cannot say where it
    saw the pointer, so there the element is asked at such a point as well. In a frame CSS turns, a click that
    has to be placed can end as `target_moved`, or when its time runs out (a card wider than a frame turned 30
    degrees): in a frame of another process Playwright counts the element's box as if the frame were not turned,
    so the pointer goes down beside the element, and in one of the page's own Playwright's checks do not pass. A
    click that is not read is Playwright's own click there, which presses what is at the point it picks and calls
    it done: what covers the frame, or the page beside the frame when the element is larger than the frame shows
    or the frame is another site's and scaled. In a frame that runs in a process of its own (another site's, or a
    sandboxed one) Playwright counts an element's box from the corner of the frame's border, so with a border or
    padding on the frame every press there, Playwright's own included, goes down up and left of the element's
    middle by that much. The element is asked whether the pointer is over it before the controller presses. One
    less than twice as wide or as high as the border and padding together is not pressed: Playwright's checks
    before a click do not find it at the point they take, and the click ends when its time does (Playwright's own
    click fails the same way from a pixel smaller). Inside a frame nested in such a frame, Playwright's own check
    is off by the outer frame's border and padding too: a button half off the inner frame's right edge was
    pressed by no way of clicking, Playwright's own included. A point carried by the two boxes there is off by as
    much, the pointer that far up and left of it. So where the element's document runs no script, a click
    Playwright places whose element's part on the screen ends within that much of where Playwright presses (a
    button half off the frame's right or lower edge) ends as `target_moved` with nothing pressed, where
    Playwright's own click presses it, and what the page shows of its own under the pointer on hover (CSS can,
    with no script), short of that point, is pressed. Where the element's document saw no move, a frame inside
    the element that ends within that much of where Playwright presses is missed, and the click ends as
    `target_moved`, as it does wherever the frame inside the element is in a closed shadow root or is a
    `fencedframe` (its document finds the root's host at the point, or a tag that is no frame's, where
    Playwright's own click presses); and a frame the page puts under the pointer once Playwright's hover has
    passed, within that much of a frame inside the element, can be pressed. The read at the press has half a
    second to set up and half a second to run; when it fails or runs past either, the press goes on, and an
    info-level log line says so.
  - Retried with the approval it asked for (the same session, the same action), an action runs as the kind
    approved, and its target is not read again before the decision: a page that had stopped answering turned the
    retry into a click that asked for nothing and left its approval unspent. At the press the button must read as
    the kind approved or as no kind of the table; a button of another kind asks again, and the approval presented
    is spent.
  - Neither read is made for an action whose own label already asks (`payment`, `destructive`, `account_change`,
    `post`): it is approved, and pressed, as the agent labelled it. Nor for an approval run by
    `POST /approvals/{id}/execute` or `browser.execute_approval`, which presses what is at the approved target
    then, whatever it reads as: only a retry of the action with its `approval_id` is read at the press.
  - A click by coordinates lands a little off its point, as a hand's does, only while the same element is under
    the moved point. Otherwise, or when that cannot be told (half a second to set the comparison up, half a
    second to make it), it lands on the point itself. Two points inside a frame of another origin, or off the
    page, are never taken for one element.
  - A press holds the mouse button down for 20 to 80 ms, as a hand does, and the browser sends no click where the
    element the press went down on is gone by the release. A page that draws the element under the pointer again
    within the hold (a list or a counter that a framework renders again on a timer, a button whose label it
    replaces) gets the press on one element and the release on its replacement: the button's handler does not
    run, and the action ends as done all the same. On a button the page replaced every 250 ms half the read
    clicks ended so (10 of 20), and on one it replaced every second about one click in nine (9 of 80); one it
    replaced every 50 ms no read click pressed at all, each ending as `target_moved` or when its time ran out,
    and a click that was not read was split there too (2 of 10). The action's `verification` does not tell such
    a press from one that worked: it says `verified: false` where nothing on the page changed, and
    `verified: true` where the press moved the focus to a button whose label alone was drawn again. Such a press
    is neither reported nor made again: a page that acts on the press itself (a menu that opens on the mouse down
    and draws its button again) sends no click either, and works, and a second press would close what the first
    opened. Read the page again before relying on what a click did.
  - It does not catch: a label the table lacks, the phrases left out above among them (the table is English, and
    has no `Place bid`, `Book now`, `Withdraw`, `Reply`, `Terminate`, `Complete payment`, `Make a payment` or
    `Apple Pay`); a label that leads up to its phrase with a word that is no icon's name ("Warning: Delete
    account"), or after an icon's name goes on in lower case ("lock delete account"); a harmless continuation that
    goes on to something the table has no phrase for ("Remove from list of admins"), and a short name ("Pay") that
    a fuller harmless name of the same button clears; a `Send` label that names another account in the list's own
    words or by a bare number ("Send code to another account", "Send code to 500"), or that says who else gets the
    code on its next line, or on the line under that one where the line between stops on no word that leaves it
    open (an address, "to my phone"), where the lines together are past 40 characters and that line opens with no
    word a label goes on with ("Send code" over "Everyone in the engineering group gets a copy", "Send code to"
    over "j***@example.com" over "and all users"), and `Resend`, which is no phrase of the table ("Resend code to
    all users"); a name that stands after the first 400 characters of a label or below its third line, and a
    phrase in a sentence under a harmless name ("Remove from library" above "and permanently delete all the
    downloaded files from the disk": a line of over 40 characters is not matched); a frame of another origin (a
    point inside one is read as the frame), and a frame that CSS scales or rotates (read at a point that is off);
    a control pressed by a key (Enter or Space on a focused button, Enter in a form field) or by a drag, and a
    `select`, whose option is chosen with no press (`select_option`); a clickable element that is not a button (a
    `div` with a handler and no role), one that chooses rather than commands (`role=option`, `tab`, `switch`,
    `menuitemradio`, `menuitemcheckbox`) and a link in no button or menu item ("How to delete your account" is an
    article); a role written after a token that is no role (`role="custom menuitem"`: a browser takes the first
    token it knows for a role, the read the first token); a button inside a closed shadow root, of which the read
    sees only the component around it; a label given only by `aria-labelledby`, by an image with no alt text, or
    by text that CSS or a canvas draws; look-alike characters (a Cyrillic "е" in "Delete"); and a page whose own
    script falsifies the read of a selector's element, or where a script or a style in motion (an animation that
    shows a button for a few milliseconds) changes what is under the pointer between the read at the press and the
    press, which are one message to the browser apart, or turns or reshapes the frame of a placed click's element
    as the pointer arrives (above). A page built for that can as well name its button
    "Continue": the read answers text that steers the model, not a site that is itself hostile.
  - It asks more than it must where a harmless label starts with a phrase and no exception lists it: `Transfer
    files`, "Send feedback", "Pay monthly", "Buy again", "Post a job", a cart's "Remove item", "Deactivate dark
    mode", a `Post` tab that only opens a form, a counter ("1 Post"), a dash where a hyphen would have joined the
    words ("Pay – as you go"), a tile whose name and description together start with one, a harmless label that
    goes on to a word of the table ("Remove filter: Pay later"). A sign-in button's `Send code` above a sentence
    that opens with any of the ten words a label goes on with (`To`, `Via`, `By`, `In`, `On`, `Now` and the rest)
    and then says anything but where the code goes ("Send code" over "To protect your account we text you a
    code"). A navigation link that is a menu item or stands in one ("Donate", "Pay a bill" or "Send money" on an
    `a` with `role=menuitem`, or in an `li` with it). A link inside a card or a row that is itself `role=button`
    or `role=menuitem`, which is read by that element's whole text and the names inside it: "Read more" in a card
    that opens "Post: release notes", "Terms apply" under "Buy 2, get 1 free", a file's name beside an icon button
    named "Delete report.pdf". And a menu item that holds its open submenu, whose commands are in its text. A
    picker button named by its field's `label` ("Transfer to", "Delete after", "Publish date"), and a button
    inside a `label` that says more ("Delete account?" beside its `Cancel`). And where an icon font's name is a
    phrase of the table on a button that does something else: Material's `remove` is the minus of a quantity
    stepper, `remove_red_eye` an eye, `publish` an upload arrow. And a short note under a sign-in button, which
    on one line with the label is no longer than a name would be: "Send code to my phone" over "Rates may apply"
    asks (37 characters together), where a longer note runs. An agent run unattended stops at each of these until
    somebody approves.
  - A target that is not there (no element at the point, an `element_id` the page no longer has) is not
    classified, and neither is one that cannot be read: a page that does not answer within 2 seconds (for an
    `element_id` or a point, 2 to set the read up and 2 more to run it), or a selector that matches nothing in
    those 2 seconds. Either way the action goes on as labelled, to succeed or fail by itself, and its press is
    read as above; when a read failed, one info-level log line names the session and the kind of failure. Inside
    a governed run's approval, which an operator gave for that very action, neither read is made.
  - A navigation off `ALLOWED_HOSTS` needs no rule of its own: with `PRE_REQUEST_BLOCKING` on (the default) it is
    refused whatever its label.
- Request-rate limiting with 429 responses and reset headers
- Metrics endpoint for scraping and alert wiring
- Automated retention cleanup for:
  - artifacts
  - uploads
  - saved auth-state files
  - decided approvals, deleted `APPROVAL_RETENTION_DAYS` (30 by default; `0` keeps them) after their decision. Kept
    whatever its age: an approval still pending, held by an action, or approved and not expired, and an operator's
    rejection in controller scope or in a session still open, which goes on refusing an agent's approval of the
    same action there, and a record whose decision time (or, for an approved one, expiry time) cannot be read.
    The log names such a record once a start, when the retention comes to read that time: one it keeps for an
    earlier reason (still pending, held by an action, or an operator's rejection that still stands) is kept
    before its time is read, and is not named.
- Containerized CI for controller tests + compose validation
- A deployment/runbook document with exact credential handoff steps
- `STEALTH_ENABLED=false`

## Non-goals for this phase

- stealth or anti-bot evasion
- full multi-tenant SaaS isolation
- SSO / RBAC / enterprise IAM
- HA-grade data plane and database failover

## Current constraints

- Docker-based isolation is appropriate for trusted single-tenant use, not hostile multi-tenant SaaS
- CAPTCHAs, MFA, and brittle login flows still require human takeover
- noVNC must sit behind a real access layer before remote use
- File + SQLite durability is acceptable for beta, not enough for larger-scale production
- Social/Veo3 integrations are extracted from the shipped controller package and are not part of the production surface.
- The dashboard accepts `#token=...` in the URL hash for one-click bookmarkable access.
  The fragment never reaches server logs, but a bookmarked or history-persisted URL
  stores the bearer token in the browser profile. On shared or remote machines,
  enter the token at the prompt instead of bookmarking it, and rotate the token
  if such a bookmark may have leaked (`API_BEARER_TOKEN`, or delete
  `data/api-token` and restart the controller to generate a new one).

## Acceptance criteria

- Local containerized test suite passes
- Compose configs render cleanly
- Startup policy validation fails closed in production mode
- Metrics and cleanup endpoints function with auth enabled
- Deployment doc is sufficient for credential handoff + live debugging
