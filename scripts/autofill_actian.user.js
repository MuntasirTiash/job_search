// ==UserScript==
// @name         Actian AI Solutions Intern — Auto-fill
// @namespace    job-search-agent
// @version      1.0
// @description  Auto-fills the Actian AI Solutions Intern application on Lever. Solve the CAPTCHA yourself and click Submit.
// @match        https://jobs.lever.co/actian/01c8697e-5880-4084-ac27-542d13306bce/apply*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    // --- Profile data ---
    const profile = {
        name:     "Muntasir Shohrab",
        email:    "ms3235@njit.edu",
        phone:    "+1 973-393-4689",
        location: "Newark, New Jersey",
        org:      "New Jersey Institute of Technology",
        linkedin: "https://www.linkedin.com/in/muntasir-shohrab-5b9086218/",
        github:   "https://github.com/MuntasirTiash",
    };

    const coverLetter = `Dear Hiring Manager,

I am excited to apply for the AI Solutions Intern role at Actian. As a PhD candidate in Business Data Science at NJIT (GPA 3.96) with hands-on experience building LLM-powered systems, I am eager to contribute to Actian's intelligent automation initiatives.

My technical background aligns directly with Actian's stack. I have built and deployed RAG pipelines using LangChain and OpenAI APIs, fine-tuned LLaMA-3 with PEFT/LoRA for domain-specific tasks, and developed Python-based NLP solutions across academic and industry settings. At Samsung R&D, I engineered production-grade computer vision pipelines, and at MetLife, I applied ML to actuarial data workflows — giving me practical experience translating AI research into real-world, API-integrated solutions.

My research further demonstrates applied impact at the intersection of AI and business. I have presented work at FMA 2025 and SFA 2025, with an additional paper under review at the Journal of Business Ethics — all leveraging NLP and financial ML to solve domain-specific problems. This blend of rigorous research and engineering experience allows me to rapidly prototype intelligent systems while maintaining production-quality standards.

I would welcome the opportunity to discuss how my LLM and RAG expertise can support Actian's AI solutions team. Thank you for your time and consideration.

Sincerely,
Muntasir Shohrab`;

    // --- Helper: fill a text input/textarea naturally ---
    function fillField(selector, value) {
        const el = document.querySelector(selector);
        if (!el || !value) return false;
        el.focus();
        el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.blur();
        return true;
    }

    // --- Wait for form to be ready ---
    function waitFor(selector, timeout = 10000) {
        return new Promise((resolve, reject) => {
            const el = document.querySelector(selector);
            if (el) return resolve(el);
            const obs = new MutationObserver(() => {
                const found = document.querySelector(selector);
                if (found) { obs.disconnect(); resolve(found); }
            });
            obs.observe(document.body, { childList: true, subtree: true });
            setTimeout(() => { obs.disconnect(); reject(new Error('Timeout: ' + selector)); }, timeout);
        });
    }

    async function run() {
        console.log('[Job Agent] Waiting for Lever form...');
        await waitFor("input[name='name']");
        // Small delay for React to finish rendering
        await new Promise(r => setTimeout(r, 800));

        fillField("input[name='name']",             profile.name);
        fillField("input[name='email']",            profile.email);
        fillField("input[name='phone']",            profile.phone);
        fillField("input[name='location']",         profile.location);
        fillField("input[name='org']",              profile.org);
        fillField("input[name='urls[LinkedIn]']",   profile.linkedin);
        fillField("input[name='urls[GitHub]']",     profile.github);
        fillField("input[name='urls[Portfolio]']",  profile.github);

        // Cover letter (if present)
        fillField("textarea[name='comments']",      coverLetter);
        fillField("textarea",                       coverLetter);  // fallback

        console.log('[Job Agent] Standard fields filled.');

        // Show banner
        const banner = document.createElement('div');
        banner.style.cssText = [
            'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:99999',
            'background:#16a34a', 'color:#fff', 'font-size:15px', 'font-weight:600',
            'padding:12px 20px', 'text-align:center', 'box-shadow:0 2px 8px rgba(0,0,0,.3)',
        ].join(';');
        banner.textContent = '✅ Form auto-filled by Job Agent — upload your resume, solve the CAPTCHA, then click Submit.';
        document.body.prepend(banner);
    }

    run().catch(err => console.error('[Job Agent] Error:', err));
})();
