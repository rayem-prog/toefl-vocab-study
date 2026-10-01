CREATE TABLE `migration_archives` (
	`owner` text NOT NULL,
	`key` text NOT NULL,
	`payload` text NOT NULL,
	PRIMARY KEY(`owner`, `key`)
);
--> statement-breakpoint
CREATE TABLE `list_records` (
	`owner` text NOT NULL,
	`key` text NOT NULL,
	`payload` text NOT NULL,
	PRIMARY KEY(`owner`, `key`)
);
--> statement-breakpoint
CREATE TABLE `photo_records` (
	`owner` text NOT NULL,
	`key` text NOT NULL,
	`payload` text NOT NULL,
	PRIMARY KEY(`owner`, `key`)
);
--> statement-breakpoint
CREATE TABLE `sync_receipts` (
	`owner` text NOT NULL,
	`id` text NOT NULL,
	`payload` text NOT NULL,
	`revision` integer NOT NULL,
	PRIMARY KEY(`owner`, `id`)
);
--> statement-breakpoint
CREATE TABLE `review_records` (
	`owner` text NOT NULL,
	`key` text NOT NULL,
	`payload` text NOT NULL,
	PRIMARY KEY(`owner`, `key`)
);
--> statement-breakpoint
CREATE TABLE `deleted_words` (
	`owner` text NOT NULL,
	`key` text NOT NULL,
	`payload` text NOT NULL,
	PRIMARY KEY(`owner`, `key`)
);
--> statement-breakpoint
CREATE TABLE `word_records` (
	`owner` text NOT NULL,
	`key` text NOT NULL,
	`payload` text NOT NULL,
	PRIMARY KEY(`owner`, `key`)
);
--> statement-breakpoint
CREATE TABLE `sync_workspace` (
	`owner` text PRIMARY KEY NOT NULL,
	`revision` integer DEFAULT 0 NOT NULL,
	`commit_token` text DEFAULT '' NOT NULL
);
