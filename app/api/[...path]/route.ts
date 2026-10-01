import {env} from 'cloudflare:workers';
import {api} from '../../../lib/api.mjs';
export const dynamic='force-dynamic';
export const GET=(request:Request)=>api(request,env);
export const POST=(request:Request)=>api(request,env);
