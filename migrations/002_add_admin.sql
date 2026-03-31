-- Phase 3: add is_admin column and remove the placeholder seed user.
-- The first real account to register will automatically become admin.

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- Delete the placeholder seed user (password was never set to a real value).
-- ON DELETE CASCADE removes their user_shows and sent_reminders rows too.
-- After running this, register a new account — it will be the admin account.
DELETE FROM users WHERE password_hash = 'placeholder';
