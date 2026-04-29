# Training Exercise: Branching & Safe Deployment

## What you'll learn
- How to make changes without touching the live site
- How to get a preview URL on Vercel for testing
- How to merge to production when you're happy

## Why this matters
Right now every change goes live instantly. After this exercise you'll have a safe build/test/deploy workflow for all your projects.

## How it works
- `main` branch = **live production** at https://tv-reminder.vercel.app
- Any other branch = **preview** — Vercel builds a separate URL automatically, nothing goes live

---

## The exercise

### Step 1 — Create a branch
```bash
cd /home/sherbert/tv-reminder
git checkout -b training-branch
```
You're now on a safe branch. Main is untouched.

### Step 2 — Make a small visible change
Open `templates/index.html` and change the page title or add a small piece of text somewhere obvious — something you'll easily spot when testing.

### Step 3 — Commit and push the branch
```bash
git add templates/index.html
git commit -m "Training: test change on branch"
git push -u origin training-branch
```

### Step 4 — Find your preview URL
Go to https://vercel.com/simeon-techbss-projects/tv-reminder  
Under **Deployments** you'll see a new deployment for `training-branch` with its own URL.  
Open it — your change is live there, production is untouched.

### Step 5 — Iterate
Make more changes, push them, refresh the preview URL. This is the build/test/build loop.

### Step 6 — Deploy to production
When happy, merge to main:
```bash
git checkout main
git merge training-branch
git push
```
Production updates. Preview URL stays around but is no longer updated.

### Step 7 — Clean up the branch
```bash
git branch -d training-branch
git push origin --delete training-branch
```

---

## Key things to remember
- Branch names can be anything — use something descriptive like `feature-dark-mode` or `fix-email-link`
- You can have multiple branches/previews at once
- Nothing reaches production until you merge to `main`
- Vercel preview URLs are public but obscure — fine for testing, don't share sensitive data on them

---

## When you're ready
Start Claude Code from this folder (`/home/sherbert/tv-reminder`) and say:  
**"I want to do the branching training exercise"**  
Claude will have this file as context and can guide you through it live.
