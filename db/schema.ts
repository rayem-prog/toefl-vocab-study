import {sqliteTable,text,integer,primaryKey} from 'drizzle-orm/sqlite-core';
export const workspace=sqliteTable('sync_workspace',{owner:text('owner').primaryKey(),revision:integer('revision').notNull().default(0),commitToken:text('commit_token').notNull().default('')});
const doc=(name:string)=>sqliteTable(name,{owner:text('owner').notNull(),key:text('key').notNull(),payload:text('payload').notNull()},t=>[primaryKey({columns:[t.owner,t.key]})]);
export const words=doc('word_records');export const lists=doc('list_records');export const reviews=doc('review_records');export const photos=doc('photo_records');export const tombstones=doc('deleted_words');export const archives=doc('migration_archives');
export const receipts=sqliteTable('sync_receipts',{owner:text('owner').notNull(),id:text('id').notNull(),payload:text('payload').notNull(),revision:integer('revision').notNull()},t=>[primaryKey({columns:[t.owner,t.id]})]);
