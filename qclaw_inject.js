// QClaw 进程内寄生转发服务器注入脚本
//
// 用途：在 QClaw 主进程内创建一个 HTTP 服务器（默认 19001），将外部请求
//       通过 QClaw 自带的 axios 实例（含签名拦截器）转发到 19000 网关。
//
// 背景：QClaw v0.2.33 的 19000 网关采用 OS 级 PID 反查机制，只允许
//       QClaw 进程树内的请求通过。外部进程即使签名正确也会被 403。
//       详见 QCLAW_19000_GATEWAY_REVERSE.md
//
// 架构：client → server.py(8083) → 19001(本脚本注入的服务器) → 19000(QClaw 网关) → 上游 LLM
//
// 使用方法：
//   1. QClaw 需以 --inspect=9229 模式启动
//   2. 运行：node qclaw_inject.js
//   3. 配置 PREFERRED_PROVIDER=qclaw-local 启动 server.py
//
// 注意事项：
//   - 只复制 axios 的请求拦截器（签名注入），不复制响应拦截器（避免流式响应循环引用）
//   - 注入可重复执行，会先关闭旧服务器再创建新服务器
//   - 不修改 QClaw 本身的任何行为，只是额外开一个 HTTP 服务器

const http = require('http');

const INSPECT_PORT = 9229;
const FORWARD_PORT = 19001;
const GATEWAY_URL = 'http://127.0.0.1:19000/proxy/llm/chat/completions';
const UPSTREAM_TIMEOUT_MS = 300000; // 5 分钟，匹配 QClaw provider timeout

/**
 * 获取 QClaw inspector 的 WebSocket 调试 URL
 */
async function getWsUrl() {
    return new Promise((resolve, reject) => {
        http.get(`http://127.0.0.1:${INSPECT_PORT}/json`, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    const targets = JSON.parse(data);
                    const target = targets.find(t => t.type === 'node') || targets[0];
                    if (!target || !target.webSocketDebuggerUrl) {
                        reject(new Error('No node inspector target found'));
                        return;
                    }
                    resolve(target.webSocketDebuggerUrl);
                } catch (e) {
                    reject(new Error(`Failed to parse inspector response: ${e.message}`));
                }
            });
        }).on('error', reject);
    });
}

async function main() {
    const wsUrl = await getWsUrl();
    console.log('[qclaw_inject] Connecting to:', wsUrl);

    const ws = new WebSocket(wsUrl);
    let msgId = 0;
    const pending = new Map();

    function send(method, params) {
        msgId++;
        return new Promise((resolve, reject) => {
            pending.set(msgId, { resolve, reject });
            ws.send(JSON.stringify({ id: msgId, method, params }));
        });
    }

    function evalCode(code) {
        return send('Runtime.evaluate', {
            expression: code,
            returnByValue: true,
            awaitPromise: true
        });
    }

    ws.addEventListener('open', async () => {
        console.log('[qclaw_inject] Connected to QClaw inspector');
        await send('Runtime.enable');

        // 关闭旧服务器（支持重复注入）
        console.log('[qclaw_inject] Closing existing forward server...');
        let r = await evalCode(`
            (async () => {
                try {
                    if (global.__qclawForwardServer) {
                        await new Promise((resolve) => {
                            global.__qclawForwardServer.close(() => resolve());
                        });
                        global.__qclawForwardServer = null;
                        return JSON.stringify({closed: true});
                    }
                    return JSON.stringify({closed: false});
                } catch(e) { return JSON.stringify({error: e.message}); }
            })()
        `);
        console.log('[qclaw_inject] Close:', r.result?.value);

        // 注入转发服务器
        console.log('[qclaw_inject] Injecting forward server...');
        r = await evalCode(`
            (async () => {
                try {
                    const http = process.getBuiltinModule('http');
                    const Module = process.mainModule.constructor;
                    const cache = Module._cache;
                    const axiosKey = Object.keys(cache).find(k => k.includes('axios') && k.endsWith('axios.cjs'));
                    if (!axiosKey) return JSON.stringify({error: 'axios not found in module cache'});
                    const qclawAxios = cache[axiosKey].exports.default || cache[axiosKey].exports;

                    // 创建干净的 axios 实例，只复制请求拦截器（签名注入），不复制响应拦截器
                    // QClaw 的响应拦截器对流式响应做 JSON.stringify 会导致循环引用
                    const cleanAxios = qclawAxios.create();
                    let reqInterceptorCount = 0;
                    if (qclawAxios.interceptors.request && qclawAxios.interceptors.request.handlers) {
                        qclawAxios.interceptors.request.handlers.forEach(h => {
                            if (h && h.fulfilled) {
                                cleanAxios.interceptors.request.use(h.fulfilled, h.rejected);
                                reqInterceptorCount++;
                            }
                        });
                    }

                    const server = http.createServer(async (req, res) => {
                        try {
                            if (req.method !== 'POST') {
                                res.writeHead(405, {'Content-Type': 'application/json'});
                                res.end(JSON.stringify({error: 'Method not allowed'}));
                                return;
                            }

                            const chunks = [];
                            for await (const chunk of req) chunks.push(chunk);
                            const bodyStr = Buffer.concat(chunks).toString('utf-8');

                            let body;
                            try {
                                body = JSON.parse(bodyStr);
                            } catch(e) {
                                res.writeHead(400, {'Content-Type': 'application/json'});
                                res.end(JSON.stringify({error: 'Invalid JSON: ' + e.message}));
                                return;
                            }

                            const authHeader = req.headers['authorization'] || '';
                            const apiKey = authHeader.replace(/^Bearer\\s+/i, '');

                            if (!apiKey) {
                                res.writeHead(401, {'Content-Type': 'application/json'});
                                res.end(JSON.stringify({error: 'Missing Authorization'}));
                                return;
                            }

                            const isStream = body.stream === true;

                            const axiosConfig = {
                                method: 'POST',
                                url: ${JSON.stringify(GATEWAY_URL)},
                                headers: {
                                    'Content-Type': 'application/json',
                                    'User-Agent': 'OpenAI/JS 6.39.1',
                                    'Authorization': 'Bearer ' + apiKey
                                },
                                data: body,
                                timeout: ${UPSTREAM_TIMEOUT_MS}
                            };

                            if (isStream) {
                                axiosConfig.responseType = 'stream';
                            }

                            // 用干净 axios 实例发请求（有签名注入，无响应拦截器）
                            const response = await cleanAxios(axiosConfig);

                            if (isStream) {
                                res.writeHead(response.status, {
                                    'Content-Type': 'text/event-stream',
                                    'Cache-Control': 'no-cache',
                                    'Connection': 'keep-alive'
                                });

                                response.data.on('data', (chunk) => {
                                    res.write(chunk);
                                });
                                response.data.on('end', () => {
                                    try { res.end(); } catch(_) {}
                                });
                                response.data.on('error', (err) => {
                                    console.error('[qclaw-fwd] stream err:', err.message);
                                    try { res.end(); } catch(_) {}
                                });

                                req.on('close', () => {
                                    if (response.data && response.data.destroy) {
                                        response.data.destroy();
                                    }
                                });
                            } else {
                                const responseData = JSON.stringify(response.data);
                                res.writeHead(response.status, {
                                    'Content-Type': 'application/json'
                                });
                                res.end(responseData);
                            }
                        } catch(e) {
                            console.error('[qclaw-fwd] err:', e.message, 'status:', e.response ? e.response.status : 'N/A');
                            const status = e.response ? e.response.status : 502;
                            let errorMsg = e.message;

                            if (e.response && e.response.data) {
                                if (typeof e.response.data.on === 'function') {
                                    try {
                                        const errChunks = [];
                                        await new Promise((resolve) => {
                                            e.response.data.on('data', (c) => errChunks.push(c));
                                            e.response.data.on('end', resolve);
                                            e.response.data.on('error', resolve);
                                            setTimeout(resolve, 3000);
                                        });
                                        errorMsg = Buffer.concat(errChunks).toString('utf-8').substring(0, 500);
                                    } catch(_) { errorMsg = 'upstream stream error'; }
                                } else {
                                    try { errorMsg = JSON.stringify(e.response.data).substring(0, 500); }
                                    catch(_) { errorMsg = 'upstream error'; }
                                }
                            }

                            try {
                                res.writeHead(status, {'Content-Type': 'application/json'});
                                res.end(JSON.stringify({error: errorMsg, status: status}));
                            } catch(_) {}
                        }
                    });

                    await new Promise((resolve, reject) => {
                        server.listen(${FORWARD_PORT}, '127.0.0.1', (err) => {
                            if (err) reject(err);
                            else resolve();
                        });
                    });

                    global.__qclawForwardServer = server;

                    return JSON.stringify({
                        success: true,
                        port: ${FORWARD_PORT},
                        reqInterceptors: reqInterceptorCount,
                        respInterceptors: 0
                    });
                } catch(e) {
                    return JSON.stringify({error: e.message, stack: e.stack});
                }
            })()
        `);
        console.log('[qclaw_inject] Result:', r.result?.value);

        if (r.result?.value && JSON.parse(r.result.value).success) {
            console.log('\n[qclaw_inject] ✅ Forward server injected successfully');
            console.log(`[qclaw_inject]    Port: ${FORWARD_PORT}`);
            console.log('[qclaw_inject]    Now set PREFERRED_PROVIDER=qclaw-local and start server.py');
        } else {
            console.error('\n[qclaw_inject] ❌ Injection failed');
            process.exit(1);
        }

        setTimeout(() => { ws.close(); process.exit(0); }, 2000);
    });

    ws.addEventListener('message', (event) => {
        const msg = JSON.parse(event.data);
        if (msg.id && pending.has(msg.id)) {
            const { resolve, reject } = pending.get(msg.id);
            pending.delete(msg.id);
            if (msg.error) reject(msg.error);
            else resolve(msg.result);
        }
    });

    ws.addEventListener('error', (err) => {
        console.error('[qclaw_inject] WebSocket error:', err.message || err);
        process.exit(1);
    });

    setTimeout(() => { console.error('[qclaw_inject] Timeout'); process.exit(1); }, 30000);
}

main();
