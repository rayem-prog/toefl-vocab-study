import html from '../lib/vocab.html?raw';
export const dynamic='force-dynamic';
export async function GET(request:Request){
 const userId=request.headers.get('oai-authenticated-user-id'),email=request.headers.get('oai-authenticated-user-email');
 if(!userId||!email)return Response.redirect(new URL('/signin-with-chatgpt?return_to=%2F',request.url),302);
 const context=JSON.stringify({userId,email}).replace(/</g,'\\u003c');
 return new Response(html.replace('<head>','<head><script>window.CLOUD_ACCOUNT='+context+';</script>'),{headers:{'Content-Type':'text/html;charset=utf-8','Cache-Control':'private,no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin'}});
}
