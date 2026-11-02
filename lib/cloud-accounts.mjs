const legacyTables={
 sync_workspace:['revision','commit_token'],
 word_records:['key','payload'],list_records:['key','payload'],
 review_records:['key','payload'],photo_records:['key','payload'],
 deleted_words:['key','payload'],migration_archives:['key','payload'],
 sync_receipts:['id','payload','revision']
};
const migrationName='legacy-owner-v1';
const normalizeEmail=value=>typeof value==='string'?value.trim().toLowerCase():'';

// The configured email comes from Sites ownership, never from a browser payload.
// Preserve the legacy namespace as an inaccessible snapshot; copy it only once.
export async function migrateLegacyAccount(db,user,configuredOwnerEmail){
 if(!normalizeEmail(configuredOwnerEmail)||normalizeEmail(user.email)!==normalizeEmail(configuredOwnerEmail))return;
 if(!user.userId||user.userId==='owner')throw Error('同步账号无效');
 const marker=()=>db.prepare('SELECT user_id FROM account_migrations WHERE name=?').bind(migrationName).first();
 if(await marker())return;
 if(!await db.prepare("SELECT owner FROM sync_workspace WHERE owner='owner'").first())return;

 const destination=await db.batch(Object.keys(legacyTables).map(table=>db.prepare(`SELECT owner FROM ${table} WHERE owner=? LIMIT 1`).bind(user.userId)));
 if(destination.some(result=>result.results.length)){
  // A concurrent migration may have finished after our initial marker read.
  if(await marker())return;
  const error=Error('旧数据迁移已暂停：当前账号已有数据，请联系维护者合并；现有数据均已保留');
  error.status=409;throw error;
 }
 const token=crypto.randomUUID();
 const guard='EXISTS (SELECT 1 FROM account_migrations WHERE name=? AND user_id=? AND commit_token=?)';
 const statements=[db.prepare('INSERT OR IGNORE INTO account_migrations(name,user_id,commit_token) VALUES (?,?,?)').bind(migrationName,user.userId,token)];
 for(const [table,columns] of Object.entries(legacyTables)){
  const fields=columns.join(',');
  statements.push(db.prepare(`INSERT INTO ${table}(owner,${fields}) SELECT ?,${fields} FROM ${table} WHERE owner='owner' AND ${guard}`).bind(user.userId,migrationName,user.userId,token));
 }
 await db.batch(statements);
}
