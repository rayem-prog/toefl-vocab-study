import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const result=spawnSync('python3',['ui-source/build_web.py','--cloud','--output',fileURLToPath(new URL('../lib/vocab.html',import.meta.url))],{stdio:'inherit'});
if(result.status!==0)process.exit(result.status||1);
