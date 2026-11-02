CREATE TABLE `account_migrations` (
	`name` text PRIMARY KEY NOT NULL,
	`user_id` text NOT NULL,
	`commit_token` text NOT NULL
);
