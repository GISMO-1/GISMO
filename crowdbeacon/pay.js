'use strict';
(() => {
  const USD_PRICE = 1.99;
  const USD_CENTS = 199;
  const MERCHANT = '5Shtc8QoF1PYVo8hzoNAsPiLN28XxezPKE1VRHDmB2cU';
  const RPC = 'https://api.mainnet-beta.solana.com';
  const MEMO_PROGRAM = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr';
  const PASS_PREFIX = 'CROWDBEACON_LIFETIME_V1';
  const STORAGE_KEY = 'crowdbeaconLifetimePassV1';
  const MIN_LAMPORTS = 1000000;

  const pay = {
    overlay: document.querySelector('#paywall'),
    close: document.querySelector('#payClose'),
    wallet: document.querySelector('#walletLine'),
    quote: document.querySelector('#solQuote'),
    status: document.querySelector('#payStatus'),
    main: document.querySelector('#payMain'),
    restore: document.querySelector('#restoreBtn'),
    receipt: document.querySelector('#receiptBox'),
    copyReceipt: document.querySelector('#copyReceipt'),
  };

  let unlocked = false;
  let connectedWallet = '';
  let quote = null;
  let provider = null;
  let connection = null;
  let busy = false;

  function setStatus(text, type = '') {
    pay.status.textContent = text;
    pay.status.className = 'paystatus' + (type ? ' ' + type : '');
  }

  function short(address) {
    return address ? address.slice(0, 5) + '…' + address.slice(-5) : 'NOT CONNECTED';
  }

  function getProvider() {
    const candidate = window.phantom?.solana || window.solana;
    return candidate?.isPhantom ? candidate : null;
  }

  function phantomBrowseUrl() {
    const here = new URL(location.href);
    here.searchParams.set('checkout', '1');
    const origin = location.origin + location.pathname;
    return 'https://phantom.app/ul/browse/' + encodeURIComponent(here.toString()) + '?ref=' + encodeURIComponent(origin);
  }

  function connectionReady() {
    if (!window.solanaWeb3) throw new Error('Solana payment library did not load.');
    if (!connection) connection = new solanaWeb3.Connection(RPC, 'confirmed');
    return connection;
  }

  async function getSolUsd() {
    const sources = [
      async () => {
        const r = await fetch('https://api.coinbase.com/v2/prices/SOL-USD/spot', { cache: 'no-store' });
        if (!r.ok) throw new Error('Coinbase quote unavailable');
        const j = await r.json();
        return Number(j?.data?.amount);
      },
      async () => {
        const r = await fetch('https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd', { cache: 'no-store' });
        if (!r.ok) throw new Error('CoinGecko quote unavailable');
        const j = await r.json();
        return Number(j?.solana?.usd);
      },
      async () => {
        const r = await fetch('https://api.kraken.com/0/public/Ticker?pair=SOLUSD', { cache: 'no-store' });
        if (!r.ok) throw new Error('Kraken quote unavailable');
        const j = await r.json();
        const first = j?.result && Object.values(j.result)[0];
        return Number(first?.c?.[0]);
      },
    ];
    for (const source of sources) {
      try {
        const value = await source();
        if (Number.isFinite(value) && value > 1) return value;
      } catch (_) {}
    }
    throw new Error('Live SOL price is unavailable. Try again in a moment.');
  }

  async function refreshQuote() {
    pay.quote.textContent = 'FETCHING LIVE SOL PRICE…';
    quote = null;
    const solUsd = await getSolUsd();
    const lamports = Math.ceil((USD_PRICE / solUsd) * solanaWeb3.LAMPORTS_PER_SOL);
    quote = { solUsd, lamports, sol: lamports / solanaWeb3.LAMPORTS_PER_SOL };
    pay.quote.textContent = quote.sol.toFixed(6) + ' SOL · $' + solUsd.toFixed(2) + '/SOL';
    updateMainButton();
    return quote;
  }

  async function deterministicReference(wallet) {
    const bytes = new TextEncoder().encode(PASS_PREFIX + '|' + wallet);
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
    return new solanaWeb3.PublicKey(digest.slice(0, 32));
  }

  function memoFor(wallet, lamports) {
    return PASS_PREFIX + '|USD=' + USD_CENTS + '|LAMPORTS=' + lamports + '|WALLET=' + wallet;
  }

  function parseMemo(logs) {
    const joined = (logs || []).join('\n');
    const index = joined.indexOf(PASS_PREFIX + '|');
    if (index < 0) return null;
    const line = joined.slice(index).split('\n')[0].replace(/["']/g, '');
    const match = line.match(/CROWDBEACON_LIFETIME_V1\|USD=(\d+)\|LAMPORTS=(\d+)\|WALLET=([1-9A-HJ-NP-Za-km-z]+)/);
    if (!match) return null;
    return { usd: Number(match[1]), lamports: Number(match[2]), wallet: match[3] };
  }

  async function verifySignature(signature, expectedWallet = '') {
    const conn = connectionReady();
    const tx = await conn.getTransaction(signature, { commitment: 'confirmed', maxSupportedTransactionVersion: 0 });
    if (!tx || tx.meta?.err) return null;
    const keys = tx.transaction.message.staticAccountKeys || tx.transaction.message.accountKeys || [];
    const keyStrings = keys.map(k => k.toBase58 ? k.toBase58() : String(k));
    const merchantIndex = keyStrings.indexOf(MERCHANT);
    if (merchantIndex < 0) return null;
    const memo = parseMemo(tx.meta?.logMessages);
    if (!memo || memo.usd !== USD_CENTS || memo.lamports < MIN_LAMPORTS) return null;
    if (expectedWallet && memo.wallet !== expectedWallet) return null;
    if (!keyStrings.includes(memo.wallet)) return null;
    const requiredSignatures = tx.transaction.message.header?.numRequiredSignatures || 1;
    if (!keyStrings.slice(0, requiredSignatures).includes(memo.wallet)) return null;
    const merchantGain = Number(tx.meta.postBalances[merchantIndex]) - Number(tx.meta.preBalances[merchantIndex]);
    if (merchantGain < memo.lamports) return null;
    const ref = await deterministicReference(memo.wallet);
    if (!keyStrings.includes(ref.toBase58())) return null;
    return { wallet: memo.wallet, signature, lamports: memo.lamports, paidAt: (tx.blockTime || 0) * 1000, reference: ref.toBase58() };
  }

  function savePass(pass) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pass));
    unlocked = true;
    connectedWallet = pass.wallet;
    pay.wallet.textContent = 'UNLOCKED · ' + short(pass.wallet);
    pay.receipt.classList.remove('hidden');
    pay.receipt.querySelector('code').textContent = pass.signature;
    pay.main.textContent = 'CREATE A GROUP';
    pay.main.dataset.action = 'create';
    setStatus('LIFETIME PASS VERIFIED ON SOLANA', 'ok');
  }

  async function verifySavedPass() {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return false;
    try {
      const saved = JSON.parse(raw);
      const verified = await verifySignature(saved.signature, saved.wallet);
      if (!verified) throw new Error('Stored receipt is invalid.');
      savePass(verified);
      return true;
    } catch (_) {
      localStorage.removeItem(STORAGE_KEY);
      unlocked = false;
      return false;
    }
  }

  async function findPassForWallet(wallet) {
    const conn = connectionReady();
    const reference = await deterministicReference(wallet);
    const signatures = await conn.getSignaturesForAddress(reference, { limit: 20 }, 'confirmed');
    for (const item of signatures) {
      if (item.err) continue;
      try {
        const pass = await verifySignature(item.signature, wallet);
        if (pass) return pass;
      } catch (_) {}
    }
    return null;
  }

  async function connectWallet() {
    provider = getProvider();
    if (!provider) {
      location.href = phantomBrowseUrl();
      return null;
    }
    const response = await provider.connect();
    connectedWallet = response.publicKey.toString();
    pay.wallet.textContent = 'PHANTOM · ' + short(connectedWallet);
    setStatus('CHECKING THIS WALLET FOR A PASS…');
    const existing = await findPassForWallet(connectedWallet);
    if (existing) {
      savePass(existing);
      return existing;
    }
    setStatus('NO LIFETIME PASS FOUND');
    updateMainButton();
    return null;
  }

  async function purchase() {
    if (busy) return;
    busy = true;
    try {
      connectionReady();
      provider = getProvider();
      if (!provider) {
        location.href = phantomBrowseUrl();
        return;
      }
      if (!connectedWallet) await connectWallet();
      if (unlocked) return;
      if (!quote) await refreshQuote();
      setStatus('BUILDING SOLANA PAYMENT…');
      const buyer = new solanaWeb3.PublicKey(connectedWallet);
      const merchant = new solanaWeb3.PublicKey(MERCHANT);
      const reference = await deterministicReference(connectedWallet);
      const transfer = solanaWeb3.SystemProgram.transfer({ fromPubkey: buyer, toPubkey: merchant, lamports: quote.lamports });
      transfer.keys.push({ pubkey: reference, isSigner: false, isWritable: false });
      const memo = new solanaWeb3.TransactionInstruction({
        programId: new solanaWeb3.PublicKey(MEMO_PROGRAM),
        keys: [],
        data: new TextEncoder().encode(memoFor(connectedWallet, quote.lamports)),
      });
      const latest = await connection.getLatestBlockhash('confirmed');
      const transaction = new solanaWeb3.Transaction({ feePayer: buyer, recentBlockhash: latest.blockhash }).add(memo, transfer);
      setStatus('APPROVE THE PAYMENT IN PHANTOM');
      const result = await provider.signAndSendTransaction(transaction);
      const signature = typeof result === 'string' ? result : result.signature;
      setStatus('WAITING FOR SOLANA CONFIRMATION…');
      await connection.confirmTransaction({ signature, blockhash: latest.blockhash, lastValidBlockHeight: latest.lastValidBlockHeight }, 'confirmed');
      let pass = null;
      for (let i = 0; i < 8 && !pass; i++) {
        await new Promise(r => setTimeout(r, 900));
        pass = await verifySignature(signature, connectedWallet);
      }
      if (!pass) throw new Error('Payment reached Solana but verification is still pending. Tap restore in a moment.');
      savePass(pass);
      notify('LIFETIME ACCESS UNLOCKED');
      setTimeout(() => { closePaywall(); createRoom(); }, 800);
    } catch (error) {
      const msg = error?.message || 'Payment was not completed.';
      setStatus(msg.toUpperCase(), 'bad');
    } finally {
      busy = false;
      updateMainButton();
    }
  }

  function updateMainButton() {
    if (busy) return;
    if (unlocked) {
      pay.main.textContent = 'CREATE A GROUP';
      pay.main.dataset.action = 'create';
      return;
    }
    provider = getProvider();
    if (!provider) {
      pay.main.textContent = 'OPEN IN PHANTOM';
      pay.main.dataset.action = 'phantom';
    } else if (!connectedWallet) {
      pay.main.textContent = 'CONNECT PHANTOM';
      pay.main.dataset.action = 'connect';
    } else if (quote) {
      pay.main.textContent = 'PAY ' + quote.sol.toFixed(6) + ' SOL';
      pay.main.dataset.action = 'pay';
    } else {
      pay.main.textContent = 'GET LIVE SOL QUOTE';
      pay.main.dataset.action = 'quote';
    }
  }

  function openPaywall() {
    pay.overlay.classList.add('open');
    document.body.classList.add('payopen');
    pay.wallet.textContent = unlocked ? 'UNLOCKED · ' + short(connectedWallet) : 'WALLET NOT CONNECTED';
    updateMainButton();
    if (!quote && !unlocked) refreshQuote().catch(e => setStatus(e.message.toUpperCase(), 'bad'));
  }

  function closePaywall() {
    pay.overlay.classList.remove('open');
    document.body.classList.remove('payopen');
  }

  async function requestCreate() {
    if (unlocked) return createRoom();
    if (await verifySavedPass()) return createRoom();
    openPaywall();
  }

  async function restore() {
    if (busy) return;
    busy = true;
    try {
      const pass = await connectWallet();
      if (!pass) setStatus('NO PURCHASE FOUND FOR THIS WALLET', 'bad');
    } catch (error) {
      setStatus((error?.message || 'Restore failed').toUpperCase(), 'bad');
    } finally {
      busy = false;
      updateMainButton();
    }
  }

  pay.main.addEventListener('click', async () => {
    const action = pay.main.dataset.action;
    if (action === 'create') { closePaywall(); createRoom(); }
    else if (action === 'phantom') location.href = phantomBrowseUrl();
    else if (action === 'connect') await connectWallet();
    else if (action === 'quote') await refreshQuote();
    else await purchase();
  });
  pay.restore.addEventListener('click', restore);
  pay.close.addEventListener('click', closePaywall);
  pay.overlay.addEventListener('click', e => { if (e.target === pay.overlay) closePaywall(); });
  pay.copyReceipt.addEventListener('click', () => {
    const signature = pay.receipt.querySelector('code').textContent;
    navigator.clipboard?.writeText(signature).then(() => notify('RECEIPT COPIED'));
  });

  window.CrowdPay = { requestCreate, openPaywall, verifySavedPass, isUnlocked: () => unlocked };
  verifySavedPass().finally(() => {
    updateMainButton();
    if (new URLSearchParams(location.search).get('checkout') === '1' && !unlocked) {
      openPaywall();
      provider = getProvider();
      if (provider) connectWallet().catch(() => {});
    }
  });
})();
