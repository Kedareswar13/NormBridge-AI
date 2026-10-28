import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { createPortal } from "react-dom";
import axios from "axios";
import "./ChatApp.css";
import SourcePopup from "../components/SourcePopup";
import PolicyPdfPopup from "../components/PolicyPdfPopup";
import PolicyDiffCompare from "../components/PolicyDiffCompare";
import ThemeToggle from "../components/ThemeToggle";
import { authHeaders, saveToken, clearToken, getToken } from "../auth";
import { streamChat } from "../streamChat";



const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Reveals the answer character by character with a blinking caret.
 * Speed adapts to length so short answers feel natural and long answers still finish in a few seconds.
 * Click the text to reveal the rest instantly.
 */
const Typewriter = ({ text = "", onComplete, onUpdate, formatFn }) => {
  const [count, setCount] = useState(0);
  const callbacks = useRef({ onComplete, onUpdate });
  callbacks.current = { onComplete, onUpdate };
  const skipRef = useRef(false);

  useEffect(() => {
    const total = text.length;
    let timer;
    let done = false;
    let lastScroll = 0;
    skipRef.current = false;
    setCount(0);
    const msPerChar = prefersReducedMotion() ? 0 : Math.max(6, Math.min(22, 6500 / Math.max(total, 1)));
    const start = performance.now();

    // setTimeout (not requestAnimationFrame) so typing keeps progressing in background tabs;
    // progress is time-based, so throttled timers simply reveal more characters per tick.
    const tick = () => {
      const now = performance.now();
      const n = skipRef.current || msPerChar === 0 ? total : Math.min(total, Math.floor((now - start) / msPerChar) + 1);
      setCount(n);
      if (now - lastScroll > 90) {
        lastScroll = now;
        callbacks.current.onUpdate && callbacks.current.onUpdate();
      }
      if (n < total) {
        timer = setTimeout(tick, 16);
      } else if (!done) {
        done = true;
        callbacks.current.onUpdate && callbacks.current.onUpdate();
        callbacks.current.onComplete && callbacks.current.onComplete();
      }
    };
    timer = setTimeout(tick, 16);
    return () => clearTimeout(timer);
  }, [text]);

  const typing = count < text.length;
  const shown = text.slice(0, count);
  return (
    <span
      className={`pp-typewriter ${typing ? "is-typing" : ""}`}
      onClick={() => { skipRef.current = true; }}
      title={typing ? "Click to show the full answer" : undefined}
    >
      {formatFn ? formatFn(shown) : shown}
      {typing && <span className="pp-caret" aria-hidden="true" />}
    </span>
  );
};

/** Shown while the backend works: mirrors the real RAG pipeline stages. */
const STAGE_STEP = { rewriting: 0, searching: 1, ranking: 2, writing: 3 };

const fmtTime = (iso) => {
  try {
    return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: true })
      .format(iso ? new Date(iso) : new Date());
  } catch { return ""; }
};

/** Server conversation -> the sidebar/chat session shape the UI already uses. */
const conversationToSession = (c) => ({
  id: c.id,
  title: c.title || "New Chat",
  timestamp: c.updated_at || c.created_at || new Date().toISOString(),
  messages: (c.messages || []).map(m => {
    const meta = m.meta || {};
    return m.role === "user"
      ? { type: "user", text: m.content, time: fmtTime(m.created_at) }
      : {
          type: "bot", text: m.content, time: fmtTime(m.created_at), isNew: false,
          sources: meta.sources || [], evaluation: meta.eval || null, fetchTime: meta.fetch_time,
          cached: !!meta.cached, rewritten: meta.rewritten_question || null, rewriteMethod: meta.rewrite_method || null,
          model: meta.model || null, chatId: meta.chat_id
        };
  })
});

const ThinkingIndicator = ({ policyCount, stage }) => {
  const writingLabel = stage && stage.model ? `Writing a grounded answer · ${stage.model}` : "Writing a grounded answer";
  const steps = useMemo(() => [
    stage && stage.stage === "rewriting" ? "Understanding your follow-up" : "Understanding your question",
    `Searching ${policyCount || "the"} SRM AP policies`,
    "Re-ranking the strongest evidence",
    writingLabel
  ], [policyCount, stage, writingLabel]);
  const [timerStep, setTimerStep] = useState(0);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const started = Date.now();
    const timer = setInterval(() => {
      const secs = Math.floor((Date.now() - started) / 1000);
      setElapsed(secs);
      setTimerStep(secs < 2 ? 0 : secs < 5 ? 1 : secs < 9 ? 2 : 3);
    }, 500);
    return () => clearInterval(timer);
  }, []);
  // real pipeline progress from the server's stream when available, otherwise a timer estimate
  const step = stage && stage.stage in STAGE_STEP ? STAGE_STEP[stage.stage] : timerStep;

  return (
    <div className="pp-thinking" role="status" aria-live="polite">
      <div className="pp-thinking-avatar"><span>P</span></div>
      <div className="pp-thinking-card">
        <div className="pp-thinking-top">
          <span className="pp-thinking-title">NormBridge AI is thinking</span>
          <span className="pp-dots"><i /><i /><i /></span>
          <span className="pp-thinking-time">{elapsed}s</span>
        </div>
        <ul className="pp-steps">
          {steps.map((s, i) => (
            <li key={s} className={i < step ? "done" : i === step ? "active" : ""}>
              <span className="pp-step-dot" />
              {s}
            </li>
          ))}
        </ul>
        <div className="pp-shimmer-line" />
      </div>
    </div>
  );
};

/** Small trust badge rendered under each answer from the backend's deterministic eval. */
const GroundingBadge = ({ evaluation }) => {
  if (!evaluation || !evaluation.verdict) return null;
  const { verdict, groundedness, source_pdfs: pdfs = [] } = evaluation;
  const label = {
    pass: "Grounded in policy",
    warn: "Partially grounded",
    fail: "Low grounding – verify with the source",
    abstained: "Not covered by the policies"
  }[verdict] || verdict;
  return (
    <div className={`pp-grounding pp-grounding-${verdict}`} title="Automatic check of the answer against the retrieved policy text">
      <span className="pp-grounding-dot" />
      <span>{label}</span>
      {verdict !== "abstained" && groundedness !== null && groundedness !== undefined && (
        <span className="pp-grounding-score">{Math.round(groundedness * 100)}%</span>
      )}
      {pdfs.length > 0 && verdict !== "abstained" && <span className="pp-grounding-src">{pdfs[0].replace(/\.pdf$/i, "").replace(/-/g, " ")}</span>}
    </div>
  );
};

function ChatApp() {
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [userEmail, setUserEmail] = useState("");
  const [otpRequired, setOtpRequired] = useState(false);
  const [otp, setOtp] = useState("");
  const [loginLoading, setLoginLoading] = useState(false);
  const [expandedEvidence, setExpandedEvidence] = useState({});


  const [sessions, setSessions] = useState([]);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  // Narrow screens start with the sidebar closed (it overlays the chat there)
  const [isSidebarOpen, setIsSidebarOpen] = useState(() => typeof window === "undefined" || window.innerWidth > 1100);
  const [isTyping, setIsTyping] = useState(false);
  const [liveText, setLiveText] = useState("");
  const [liveStage, setLiveStage] = useState(null);


  const [popupSource, setPopupSource] = useState(null);
  const [policies, setPolicies] = useState([]);
  const [showPolicies, setShowPolicies] = useState(false);
  const [showDiffCompare, setShowDiffCompare] = useState(false);


  const [selectedPolicy, setSelectedPolicy] = useState(null);
  const [showPdfPopup, setShowPdfPopup] = useState(false);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [pdfError, setPdfError] = useState(null);


  const [showFeedback, setShowFeedback] = useState(false);
  const [feedbackStars, setFeedbackStars] = useState(0);
  const [feedbackText, setFeedbackText] = useState("");
  const [feedbackSubmitted, setFeedbackSubmitted] = useState(false);
  const [hasSubmittedFeedback, setHasSubmittedFeedback] = useState(false);


  const [sidebarSearch, setSidebarSearch] = useState("");
  const [chatSearch, setChatSearch] = useState("");
  const [currentHitIndex, setCurrentHitIndex] = useState(-1);
  const [totalHits, setTotalHits] = useState(0);

  const [greetingText, setGreetingText] = useState("");
  const [showLogoutBye, setShowLogoutBye] = useState(false);
  const [selectedModel, setSelectedModel] = useState("");
  // Installed chat models reported by the server (/health); the default model answers when nothing else is chosen.
  const [chatModels, setChatModels] = useState(["phi3:mini"]);
  const [defaultModel, setDefaultModel] = useState("");
  const [showModelMenu, setShowModelMenu] = useState(false);
  const modelMenuRef = useRef(null);
  const [showScrollDown, setShowScrollDown] = useState(false);
  const [modelStats, setModelStats] = useState(() => {
    const saved = localStorage.getItem("modelStats");
    return saved ? JSON.parse(saved) : {};
  });

  useEffect(() => {
    axios.get(`${process.env.REACT_APP_API_URL || "http://localhost:5001"}/health`)
      .then(res => {
        const def = res.data?.default_model;
        const list = res.data?.chat_models?.length ? res.data.chat_models : (def ? [def] : []);
        if (def) { setDefaultModel(def); setSelectedModel(prev => (list.includes(prev) ? prev : def)); }
        if (list.length) setChatModels(list);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!showModelMenu) return undefined;
    const close = (e) => { if (modelMenuRef.current && !modelMenuRef.current.contains(e.target)) setShowModelMenu(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [showModelMenu]);
  const [speakingIndex, setSpeakingIndex] = useState(null);
  const [copiedIndex, setCopiedIndex] = useState(null);
  const copyAnswer = (text, index) => {
    const done = () => { setCopiedIndex(index); setTimeout(() => setCopiedIndex(null), 1600); };
    if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done).catch(() => {});
    else done();
  };
  const speechRef = useRef(null);
  const [isListening, setIsListening] = useState(false);
  const recognitionRef = useRef(null);

  useEffect(() => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SpeechRecognition) {
      const recognition = new SpeechRecognition();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.lang = 'en-US';

      recognition.onstart = () => setIsListening(true);
      recognition.onend = () => setIsListening(false);
      recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript;
        setQ(transcript);



        send(transcript);
      };
      recognition.onerror = () => setIsListening(false);

      recognitionRef.current = recognition;
    }

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleListening = () => {
    if (isListening) {
      recognitionRef.current?.stop();
    } else {
      window.speechSynthesis.cancel(); 
      recognitionRef.current?.start();
    }
  };

  const handleSpeak = (text, index) => {

    if (speakingIndex === index) {
      window.speechSynthesis.cancel();
      setSpeakingIndex(null);
      return;
    }


    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);


    const voices = window.speechSynthesis.getVoices();
    const premiumVoice = voices.find(v =>
      v.name.includes("Google UK English Female") ||
      v.name.includes("Serena")
    );
    if (premiumVoice) utterance.voice = premiumVoice;

    utterance.rate = 1.0;
    utterance.pitch = 1.0;

    utterance.onend = () => setSpeakingIndex(null);
    utterance.onerror = () => setSpeakingIndex(null);

    setSpeakingIndex(index);
    window.speechSynthesis.speak(utterance);
    speechRef.current = utterance;
  };

  const [suggestions] = useState([
    "What is the minimum attendance required to write the end semester exam?",
    "By what time must hostel residents return every night?",
    "Is the hostel caution deposit refundable?",
    "What is the library fine for an overdue book?"
  ]);

  const messagesEndRef = useRef(null);
  const chatWindowRef = useRef(null);


  const inputRef = useRef(null);


  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = "auto";
      inputRef.current.style.height = `${Math.min(inputRef.current.scrollHeight, 150)}px`;
    }
  }, [q]);

  const createNewChat = useCallback(() => {
    const newSession = {
      id: Date.now().toString(),
      title: "New Chat",
      messages: [],
      timestamp: new Date().toISOString()
    };
    setSessions(prev => [newSession, ...prev]);
    setCurrentSessionId(newSession.id);
    setError("");
    setQ("");
  }, []);

  const switchSession = (id) => {
    setCurrentSessionId(id);
    setError("");
    setQ("");
  };

  const deleteSession = (e, id) => {
    e.stopPropagation();
    axios.delete(`${process.env.REACT_APP_API_URL || "http://localhost:5001"}/conversations/${encodeURIComponent(id)}`,
      { headers: authHeaders() }).catch(() => {});
    setSessions(prev => {
      const filtered = prev.filter(s => s.id !== id);
      if (id === currentSessionId) {
        if (filtered.length > 0) {
          setCurrentSessionId(filtered[0].id);
        } else {

          const fresh = {
            id: Date.now().toString(),
            title: "New Chat",
            messages: [],
            timestamp: new Date().toISOString()
          };
          setCurrentSessionId(fresh.id);
          return [fresh];
        }
      }
      return filtered;
    });
  };

  const updateSessionMessages = (sessionId, newMessages) => {
    setSessions(prev => prev.map(s => {
      if (s.id === sessionId) {

        let title = s.title;
        if (s.messages.length === 0 && newMessages.length > 0) {
          const firstMsg = newMessages[0].text;
          title = firstMsg.length > 30 ? firstMsg.substring(0, 30) + "..." : firstMsg;
        }
        return { ...s, messages: newMessages, title };
      }
      return s;
    }));
  };




  useEffect(() => {
    if (isAuthenticated && userEmail && isSidebarOpen) {

      const match = userEmail.match(/^([^@._]+)/);
      const namePart = match ? match[1] : "User";
      const firstName = namePart.charAt(0).toUpperCase() + namePart.slice(1).toLowerCase();
      const fullGreeting = `Hi ${firstName}!`;

      let i = 0;
      setGreetingText("");
      const timer = setInterval(() => {
        if (i < fullGreeting.length) {
          setGreetingText(fullGreeting.slice(0, i + 1));
          i++;
        } else {
          clearInterval(timer);
        }
      }, 100);
      return () => clearInterval(timer);
    } else if (!isSidebarOpen) {
      setGreetingText(""); 
    }
  }, [isAuthenticated, userEmail, isSidebarOpen]);


  const conversation = useMemo(() => sessions.find(s => s.id === currentSessionId)?.messages || [], [sessions, currentSessionId]);

  const modelRef = useRef(selectedModel);
  const sessionIdRef = useRef(currentSessionId);
  const conversationRef = useRef(conversation);


  useEffect(() => { modelRef.current = selectedModel; }, [selectedModel]);
  useEffect(() => { sessionIdRef.current = currentSessionId; }, [currentSessionId]);
  useEffect(() => { conversationRef.current = conversation; }, [conversation]);


  const scrollToBottom = useCallback(() => {
    if (chatWindowRef.current) {
      chatWindowRef.current.scrollTop = chatWindowRef.current.scrollHeight;
    }
    const msgContainer = document.querySelector('.messages-container');
    if (msgContainer) {
      msgContainer.scrollTop = msgContainer.scrollHeight;
    }
    const mainArea = document.querySelector('.chat-main-area');
    if (mainArea) {
      mainArea.scrollTop = mainArea.scrollHeight;
    }
  }, []);

  // Show the centred "jump to latest" arrow only when the user has scrolled up.
  useEffect(() => {
    const el = chatWindowRef.current;
    if (!el) return undefined;
    const onScroll = () => setShowScrollDown(el.scrollHeight - el.scrollTop - el.clientHeight > 160);
    onScroll();
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  });

  useEffect(() => {

    if (!chatSearch.trim()) {
      scrollToBottom();
    }

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation, isTyping, scrollToBottom]);


  useEffect(() => {
    if (!chatSearch.trim()) {
      setTotalHits(0);
      setCurrentHitIndex(-1);
      return;
    }

    const escaped = chatSearch.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    let count = 0;
    try {
      const regex = new RegExp(escaped, 'gi');
      conversation.forEach((msg, i) => {
        if (msg.text) {
          const matches = msg.text.match(regex);
          if (matches) count += matches.length;
        }


        if (msg.sources) {
          let hasSourceMatch = false;
          msg.sources.forEach(src => {
            const srcText = (src.text_snippet || src.text || "");
            const srcMatches = srcText.match(regex);
            if (srcMatches) {
              count += srcMatches.length;
              hasSourceMatch = true;
            }
          });

          if (hasSourceMatch && !expandedEvidence[i]) {
            setExpandedEvidence(prev => ({ ...prev, [i]: true }));
          }
        }
      });
    } catch (e) {
      console.warn("Search error:", e);
    }

    setTotalHits(count);
    setCurrentHitIndex(count > 0 ? 0 : -1);
  }, [chatSearch, conversation, expandedEvidence]);

  const navigateSearch = (direction) => {
    if (totalHits === 0) return;
    if (direction === 'next') {
      setCurrentHitIndex(prev => (prev + 1) % totalHits);
    } else {
      setCurrentHitIndex(prev => (prev - 1 + totalHits) % totalHits);
    }
  };

  const handleSearchKeyDown = (e) => {
    if (totalHits === 0) return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      navigateSearch('next');
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      navigateSearch('prev');
    } else if (e.key === "Enter") {
      e.preventDefault();
      navigateSearch('next');
    } else if (e.key === "Escape") {
      setChatSearch("");
    }
  };


  useEffect(() => {
    if (currentHitIndex !== -1) {
      const highlights = document.querySelectorAll('.search-highlight');
      const active = highlights[currentHitIndex];
      if (active) {
        active.scrollIntoView({ behavior: 'smooth', block: 'center' });

        active.classList.add('highlight-focused');
        setTimeout(() => active.classList.remove('highlight-focused'), 1000);
      }
    }
  }, [currentHitIndex, chatSearch]);

  useEffect(() => {
    let savedEmail = localStorage.getItem("userEmail");
    if (savedEmail && !getToken()) {
      // signed in before session tokens existed (or the token was cleared): sign in again
      localStorage.removeItem("userEmail");
      savedEmail = null;
    }
    if (savedEmail) {
      setUserEmail(savedEmail);
      setIsAuthenticated(true);


      axios.post(`${process.env.REACT_APP_API_URL || "http://localhost:5001"}/auth/check`, { email: savedEmail })
        .then(res => {
          if (res.data.valid) {
            setHasSubmittedFeedback(res.data.hasSubmittedFeedback);
            if (res.data.hasSubmittedFeedback) {
              localStorage.setItem(`feedback_${savedEmail}`, "true");
            }
          }
        })
        .catch(err => console.error("Sync error:", err));
      // conversation history is loaded by the history effect below (same path as after a fresh sign-in)
    }


    axios.get(`${process.env.REACT_APP_API_URL || "http://localhost:5001"}/policies`, { headers: authHeaders() })
      .then(res => setPolicies(res.data))
      .catch(() => setPolicies([]));
  }, [createNewChat]);


  // ---------------------------------------------------------------- conversation history (server = source of truth)
  // Runs after EVERY sign-in and on refresh. Previously chats lived only in localStorage and were read only on page
  // load, so signing in again started from an empty list that then overwrote the saved chats.
  const historyLoadedFor = useRef(null);
  const [historyReady, setHistoryReady] = useState(false);

  useEffect(() => {
    if (!isAuthenticated || !userEmail || !getToken() || historyLoadedFor.current === userEmail) return;
    historyLoadedFor.current = userEmail;
    setHistoryReady(false);
    let cancelled = false;
    const api = process.env.REACT_APP_API_URL || "http://localhost:5001";
    let local = [];
    try { local = JSON.parse(localStorage.getItem(`chatSessions_${userEmail}`) || "[]") || []; } catch { local = []; }
    if (local.length) {           // instant paint from the local cache while the server answers
      setSessions(local);
      setCurrentSessionId(local[0].id);
    }
    (async () => {
      try {
        const withMessages = local.filter(s => (s.messages || []).length > 0);
        if (withMessages.length) {  // one-time (idempotent) upload of chats that only existed in this browser
          await axios.post(`${api}/conversations/import`, { sessions: withMessages }, { headers: authHeaders() });
        }
        const res = await axios.get(`${api}/conversations`, { headers: authHeaders() });
        if (cancelled) return;
        const server = (res.data.conversations || []).map(conversationToSession);
        if (server.length) {
          setSessions(server);
          setCurrentSessionId(server[0].id);
        } else if (!local.length) {
          createNewChat();
        }
      } catch (e) {
        console.error("Could not load chat history:", e);
        if (!local.length) createNewChat();
      } finally {
        if (!cancelled) setHistoryReady(true);
      }
    })();
    return () => { cancelled = true; };
  }, [isAuthenticated, userEmail, createNewChat]);


  useEffect(() => {
    if (isAuthenticated && !hasSubmittedFeedback && userEmail) {
      if (!localStorage.getItem(`feedback_${userEmail}`)) {
        const timer = setTimeout(() => setShowFeedback(true), 15000);
        return () => clearTimeout(timer);
      }
    }
  }, [isAuthenticated, userEmail, hasSubmittedFeedback]);


  useEffect(() => {
    // local cache only after the server history is in, so an empty list can never overwrite saved chats
    if (isAuthenticated && userEmail && historyReady && sessions.length > 0) {
      localStorage.setItem(`chatSessions_${userEmail}`, JSON.stringify(sessions));
    }
  }, [sessions, isAuthenticated, userEmail, historyReady]);


  const handlePolicyClick = async (policy) => {
    try {
      const normalizedPolicy = typeof policy === 'string'
        ? { name: policy, page: 0 }
        : {
            name: policy?.name || policy?.pdf || policy?.filename || '',
            page: policy?.page !== undefined ? policy.page : 0,
            highlightText: policy?.highlightText || policy?.text_snippet || policy?.text || ''
          };

      setSelectedPolicy(normalizedPolicy);
      setShowPdfPopup(true);
      setPdfLoading(false);
      setPdfError(null);
    } catch (error) {
      console.error("Error preparing PDF view:", error);
      setPdfError(error.message || "Failed to load PDF");
      setPdfLoading(false);
    }
  };


  const handleClosePdfPopup = () => {
    setShowPdfPopup(false);
    setSelectedPolicy(null);
    setPdfError(null);
  };

  const handleEmailSubmit = async (e) => {
    if (e && e.preventDefault) e.preventDefault();
    setLoginLoading(true);
    if (otpRequired) {
      try {
        const response = await axios.post(`${process.env.REACT_APP_API_URL || "http://localhost:5001"}/auth/validate`, {
          email: userEmail,
          otp: otp
        });

        if (response.data.valid) {
          saveToken(response.data.token);
          setIsAuthenticated(true);
          setError(""); 
          localStorage.setItem("userEmail", userEmail);
          setHasSubmittedFeedback(response.data.hasSubmittedFeedback);

          if (response.data.hasSubmittedFeedback) {
            localStorage.setItem(`feedback_${userEmail}`, "true");
          }

          localStorage.setItem(`otp_verified_${userEmail}`, Date.now().toString());
        }
      } catch (error) {
        setError(error.response?.data?.error || "Invalid OTP. Please try again.");
      } finally {
        setLoginLoading(false);
      }
    } else {
      setError("");


      const lastVerified = localStorage.getItem(`otp_verified_${userEmail}`);
      if (lastVerified && getToken()) {
        const timePassed = Date.now() - parseInt(lastVerified);
        const twoHours = 2 * 60 * 60 * 1000;

        if (timePassed < twoHours) {
          setIsAuthenticated(true);
          localStorage.setItem("userEmail", userEmail);
          setLoginLoading(false);
          return; 
        }
      }

      try {
        const response = await axios.post(`${process.env.REACT_APP_API_URL || "http://localhost:5001"}/auth/request-otp`, {
          email: userEmail
        });

        if (response.data.bypass) {
          saveToken(response.data.token);
          setIsAuthenticated(true);
          localStorage.setItem("userEmail", userEmail);

          if (response.data.hasSubmittedFeedback) {
            setHasSubmittedFeedback(true);
            localStorage.setItem(`feedback_${userEmail}`, "true");
          }

          localStorage.setItem(`otp_verified_${userEmail}`, Date.now().toString());
          return;
        }

        if (response.data.success) {
          setOtpRequired(true);
          setError("OTP sent successfully!");
        }
      } catch (error) {
        setError(error.response?.data?.error || "Invalid email domain. Please use @srmap.edu.in");
      } finally {
        setLoginLoading(false);
      }
    }
  };

  const handleLoginKeyDown = (e) => {
    if (e.key === 'Enter') {
      handleEmailSubmit(e);
    }
  };

  const handleSuggestionClick = (s) => {
    setQ(s);

    setTimeout(() => {
      const btn = document.getElementById('send-btn');
      if (btn) btn.click();
    }, 50);
  };




  const closeSourcePopup = () => {
    setPopupSource(null);
  };


  const handleReportInaccuracy = (msgIndex) => {
    alert("Thank you for your feedback! This response has been flagged for review by the administrators to improve accuracy.");
  };

  const send = async (voiceQuestion = null) => {

    const isVoice = typeof voiceQuestion === 'string';
    const questionToAsk = isVoice ? voiceQuestion : q;

    if (!questionToAsk || typeof questionToAsk !== 'string' || !questionToAsk.trim()) {
      setError("Please enter a question");
      return;
    }


    let sessionId = sessionIdRef.current;
    let currentMessages = conversationRef.current;

    if (!sessionId) {
      const newSession = {
        id: Date.now().toString(),
        title: "New Chat",
        messages: [],
        timestamp: new Date().toISOString() 
      };
      setSessions(prev => [newSession, ...prev]);
      setCurrentSessionId(newSession.id);
      sessionId = newSession.id;
      currentMessages = [];

      sessionIdRef.current = sessionId;
    }

    const userMessage = {
      type: "user",
      text: questionToAsk,
      time: new Intl.DateTimeFormat('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit',
        minute: '2-digit',
        hour12: true
      }).format(new Date()),
      isNew: true
    };

    const updatedMessages = [...currentMessages, userMessage];
    updateSessionMessages(sessionId, updatedMessages);

    setLoading(true);
    setError("");

    const question = questionToAsk;
    setQ("");

    try {
      const startTime = performance.now();
      const currentMod = modelRef.current; 
      // last two exchanges, so follow-ups like "and for PhD students?" can be rewritten into standalone questions
      const history = currentMessages
        .filter(m => (m.type === "user" || m.type === "bot") && m.text)
        .slice(-4)
        .map(m => ({ role: m.type === "user" ? "user" : "assistant", text: String(m.text).slice(0, 600) }));
      const body = { question: question, model: currentMod, history, session_id: sessionId };
      let res;
      let streamed = false;
      setLiveText("");
      setLiveStage(null);
      try {
        // token streaming (SSE); the final payload is identical to POST /chat
        const out = await streamChat(body, {
          onStage: (s) => setLiveStage(s),
          onToken: (t) => { streamed = true; setLiveText(prev => prev + t); scrollToBottom(); },
        });
        if (out.status >= 400) {
          const err = new Error(`chat failed: ${out.status}`);
          err.response = { status: out.status, data: out.data };
          throw err;
        }
        res = { data: out.data };
      } catch (streamError) {
        if (streamError.response) throw streamError;
        // streaming unavailable (old server / proxy): fall back to the plain JSON endpoint
        res = await axios.post(`${process.env.REACT_APP_API_URL || "http://localhost:5001"}/chat`, body, { headers: authHeaders() });
      }
      const endTime = performance.now();
      const fetchTime = ((endTime - startTime) / 1000).toFixed(2);

      if (!streamed) {
        setIsTyping(true);
        await new Promise(r => setTimeout(r, 250));
      }

      const botMessage = {
        type: "bot",
        text: res.data.answer,
        time: new Intl.DateTimeFormat('en-IN', {
          timeZone: 'Asia/Kolkata',
          hour: '2-digit',
          minute: '2-digit',
          hour12: true
        }).format(new Date()),
        fetchTime: fetchTime,
        sources: res.data.sources || [],
        evaluation: res.data.eval || null,
        cached: !!res.data.cached,
        rewritten: res.data.rewritten_question || null,
        rewriteMethod: res.data.rewrite_method || null,
        model: res.data.model || null,
        // a streamed answer was already watched live: show the final text directly instead of replaying it
        isNew: !streamed,
        chatId: res.data.chat_id
      };


      setModelStats(prev => {
        const stats = prev[currentMod] || { total: 0, count: 0, avg: 0 };
        const newTotal = stats.total + parseFloat(fetchTime);
        const newCount = stats.count + 1;
        const newAvg = (newTotal / newCount).toFixed(2);
        const updated = {
          ...prev,
          [currentMod]: { total: newTotal, count: newCount, avg: parseFloat(newAvg) }
        };

        localStorage.setItem("modelStats", JSON.stringify(updated));
        return updated;
      });

      updateSessionMessages(sessionId, [...updatedMessages, botMessage]);
      setLiveText("");
      setLiveStage(null);
      setIsTyping(false);



      if (document.activeElement?.className?.includes('mic-btn') || isListening) {
        handleSpeak(res.data.answer, updatedMessages.length);
      }

    } catch (error) {
      if (error.response?.status === 401) {
        setError("Your session has expired. Please sign in again.");
        historyLoadedFor.current = null;
        setIsAuthenticated(false);
        localStorage.removeItem("userEmail");
        clearToken();
      } else if (error.response?.status === 403) {
        setError("Access denied. Please login with @srmap.edu.in email");
        setIsAuthenticated(false);
        localStorage.removeItem("userEmail");
        clearToken();
      } else {
        setError("Backend not responding. Please make sure the server is running.");
      }
    } finally {
      setLoading(false);
      setLiveText("");
      setLiveStage(null);
    }
  };

  const handleKeyPress = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !loading) {
      e.preventDefault();
      send();
    }
  };

  const handleSatisfaction = async (chatId, satisfaction, msgIndex) => {
    try {
      await axios.post(`${process.env.REACT_APP_API_URL || `${process.env.REACT_APP_API_URL || "http://localhost:5001"}`}/chat/${chatId}/satisfaction`, {
        satisfaction: satisfaction
      }, { headers: authHeaders() });

      setSessions(prev => prev.map(s => {
        if (s.id === currentSessionId) {
          const updatedMessages = [...s.messages];
          if (updatedMessages[msgIndex]) {
            updatedMessages[msgIndex].satisfactionGiven = satisfaction;
          }
          return { ...s, messages: updatedMessages };
        }
        return s;
      }));
    } catch (e) {
      console.error("Error saving satisfaction:", e);
    }
  };


  const handleLogout = () => {
    setShowLogoutBye(true);
  };

  const confirmLogout = () => {
    setIsAuthenticated(false);
    setUserEmail("");
    if (userEmail) localStorage.removeItem(`chatSessions_${userEmail}`);  // the server keeps them; don't leave chats on a shared PC
    localStorage.removeItem("userEmail");
    clearToken();



    window.location.reload();
  };

  const formatText = (text, sources = [], chatSearchText = chatSearch) => {
    if (!text) return "";

    const lines = text.split("\n");
    const escapedSearch = chatSearchText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

    return lines.map((l, i) => {
      let content = l;
      const footnoteRegex = /\[(\d+)\]/g;
      const parts = l.split(footnoteRegex);

      content = parts.map((part, index) => {
        if (index % 2 === 1) { 
          const num = parseInt(part);
          const source = sources[num - 1];
          if (source) {
            return (
              <sup key={index} className="footnote-sup"
                onClick={() => handlePolicyClick({
                  name: source.pdf,
                  highlightText: source.text_snippet || source.text,
                  page: source.page
                })}
                title={`Source: ${source.pdf} - Click to open`}>
                [{num}]
              </sup>
            );
          }
          return `[${part}]`;
        }


        if (chatSearchText.trim() && part.toLowerCase().includes(chatSearchText.toLowerCase())) {
          const subParts = part.split(new RegExp(`(${escapedSearch})`, 'gi'));
          return subParts.map((sub, sIdx) =>
            sub.toLowerCase() === chatSearchText.toLowerCase() ?
              <span key={sIdx} className="search-highlight">{sub}</span> : sub
          );
        }
        return part;
      });

      return (
        <React.Fragment key={i}>
          {content}
          {i !== lines.length - 1 && <br />}
        </React.Fragment>
      );
    });
  };

  const submitFeedback = async () => {
    if (feedbackStars === 0) return;
    try {
      await axios.post(`${process.env.REACT_APP_API_URL || "http://localhost:5001"}/feedback`, {
        stars: feedbackStars,
        review: feedbackText
      }, { headers: authHeaders() });
      localStorage.setItem(`feedback_${userEmail}`, "true");
      setFeedbackSubmitted(true);
      setHasSubmittedFeedback(true);
      setTimeout(() => setShowFeedback(false), 2000);
    } catch (e) {
      console.error("Error submitting feedback:", e);
    }
  };


  if (!isAuthenticated) {
    return (
      <div className="app-container page-entrance">
        <div className="email-login-card">

          <div className="login-branding">
            <img
              src="/plogo.png"
              alt="SRM University"
              style={{ objectFit: 'contain' }}
            />
            <h2>NormBridge AI</h2>
            <p>Your university policy assistant: plain-English questions answered from the official rules, with page-level evidence.</p>
          </div>


          <div className="login-form-panel">
            <div className="login-header" style={{ width: '100%', maxWidth: '360px', marginTop: '40px' }}>
              <h2 style={{ fontSize: '22px', fontWeight: '600', marginBottom: '6px', color: '#0f172a' }}>Sign In</h2>
              <p style={{ fontSize: '14px', color: '#64748b', margin: '0 0 24px 0' }}>Access with your university email</p>
              <div className="accent-line"></div>
            </div>

            <form onSubmit={handleEmailSubmit} className="login-form" style={{ width: '100%', maxWidth: '360px' }}>
              <div className="form-group">
                <label>University Email</label>
                <input
                  type="email"
                  value={userEmail}
                  onChange={(e) => setUserEmail(e.target.value)}
                  placeholder="username@srmap.edu.in"
                  className="email-input"
                  required
                  pattern=".*@srmap\.edu\.in$"
                  title="Must be @srmap.edu.in email"
                  disabled={otpRequired || loginLoading}
                  onKeyDown={handleLoginKeyDown}
                />
              </div>

              {otpRequired && (
                <div className="form-group" style={{ marginTop: '15px' }}>
                  <label>One-Time Password (OTP)</label>
                  <input
                    type="password"
                    value={otp}
                    onChange={(e) => setOtp(e.target.value)}
                    placeholder="Enter 6-digit OTP"
                    className="email-input"
                    required
                    maxLength={6}
                    disabled={loginLoading}
                    onKeyDown={handleLoginKeyDown}
                  />
                  <div style={{ fontSize: '12px', marginTop: '5px', cursor: 'pointer', color: '#0f172a' }}
                    onClick={() => { setOtpRequired(false); setOtp(""); setError(""); }}>
                    Change Email
                  </div>
                </div>
              )}

              {error && (
                <div className="error-message" style={{ color: error.includes("successfully") ? 'green' : '' }}>
                  {error.includes("successfully") ? '✅ ' : '⚠️ '} {error}
                </div>
              )}

              <button type="submit" className={`login-btn ${loginLoading ? 'loading' : ''}`} disabled={loginLoading}>
                {loginLoading ? (
                  <span className="btn-spinner"></span>
                ) : (
                  otpRequired ? "Verify OTP" : "Continue"
                )}
              </button>
            </form>

            <div className="login-footer" style={{ width: '100%', maxWidth: '360px' }}>
              <p>Only @srmap.edu.in emails are allowed</p>
              <p className="admin-hint">
                Admin? <a href="/admin" onClick={(e) => { e.preventDefault(); window.location.href = '/admin'; }}>Login here</a>
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }




  return (
    <div className="app-container">

      <aside className={`chat-sidebar ${isSidebarOpen ? 'open' : ''}`}>
        <div className="sidebar-header">
          <div className="sidebar-greeting">
            {greetingText}
            {isSidebarOpen && <span className="typewriter-cursor">|</span>}
          </div>
          <button className="sidebar-toggle-btn" onClick={() => setIsSidebarOpen(false)}>
            <span className="toggle-icon">◀</span>
          </button>
        </div>

        <div className="sidebar-search-container">
          <input
            type="text"
            placeholder="Search chats..."
            value={sidebarSearch}
            onChange={(e) => setSidebarSearch(e.target.value)}
            className="sidebar-search-input"
          />
        </div>

        <div className="sidebar-sessions">
          <div className="session-group-label">Recent Chats</div>
          {sessions
            .filter(s => s.title.toLowerCase().includes(sidebarSearch.toLowerCase()))
            .map(session => (
              <div
                key={session.id}
                className={`session-item ${session.id === currentSessionId ? 'active' : ''}`}
                onClick={() => switchSession(session.id)}
              >
                <span className="session-icon">💬</span>
                <span className="session-title">{session.title}</span>
                <button className="delete-session-btn" onClick={(e) => deleteSession(e, session.id)} title="Delete Chat">
                  🗑️
                </button>
              </div>
            ))}
        </div>

        <div className="sidebar-footer">
          <div className="user-profile-row">
            <span className="user-avatar-icon">👤</span>
            <span className="user-name-text">
              {(() => {
                const localPart = userEmail.split('@')[0];
                return localPart
                  .split(/[._]/)
                  .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
                  .join(' ');
              })()}
            </span>
          </div>
          <button className="logout-sidebar-btn" onClick={handleLogout}>
            Logout
          </button>
        </div>
      </aside>


      {!isSidebarOpen && (
        <button className="sidebar-open-btn" onClick={() => setIsSidebarOpen(true)}>
          <span className="toggle-icon">▶</span>
        </button>
      )}

      <div className="chat-container">
        <div className="chat-main-area">


          {popupSource && (
            <SourcePopup
              source={popupSource}
              onClose={closeSourcePopup}
              onOpenPdf={(src) => {
                closeSourcePopup();
                handlePolicyClick({ name: src.pdf, highlightText: src.text_snippet, page: src.page });
              }}
            />
          )}


          {showPdfPopup && (
            <PolicyPdfPopup
              policy={selectedPolicy}
              onClose={handleClosePdfPopup}
              loading={pdfLoading}
              error={pdfError}
            />
          )}


          {showDiffCompare && (
            <PolicyDiffCompare
              policies={policies}
              credentials={{ username: "", password: "" }}
              onClose={() => setShowDiffCompare(false)}
            />
          )}


          <header className="chat-header">
            <div className="header-content">
              <div className="title-group">
                <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                  <img
                    src="/plogo.png"
                    alt="SRM Logo"
                    style={{
                      width: '36px',
                      height: '36px',
                      objectFit: 'contain'
                    }}
                  />
                  <span className="app-title">
                    <span className="app-title-main">NormBridge</span>
                    <span className="app-title-accent"> AI</span>
                  </span>
                  <button
                    className={`policy-bulb-btn ${showPolicies ? 'active' : ''}`}
                    title="Show available policies"
                    onClick={() => setShowPolicies(!showPolicies)}
                  >
                    💡
                  </button>
                </div>
              </div>
            </div>
            <div className="header-actions">
              <div className="chat-search-container">
                <i className="fas fa-search search-icon-left"></i>
                <input
                  type="text"
                  placeholder="Search in chat..."
                  value={chatSearch}
                  onChange={(e) => setChatSearch(e.target.value)}
                  onKeyDown={handleSearchKeyDown}
                  className="chat-search-input"
                  aria-label="Search in current chat"
                />

                {chatSearch && (
                  <div className={`search-results-tab ${totalHits >= 0 ? 'visible' : ''}`}>
                    <span className="search-stats">
                      {totalHits > 0 ? `${currentHitIndex + 1} of ${totalHits}` : 'No results'}
                    </span>
                    <div className="search-nav-btns">
                      <button
                        className="search-nav-btn"
                        onClick={() => navigateSearch('prev')}
                        disabled={totalHits === 0}
                        title="Previous match"
                      >
                        <i className="fas fa-chevron-up"></i>
                      </button>
                      <button
                        className="search-nav-btn nav-btn-primary"
                        onClick={() => navigateSearch('next')}
                        disabled={totalHits === 0}
                        title="Next match (Apparent)"
                      >
                        <i className="fas fa-chevron-down"></i>
                      </button>
                    </div>
                    <div className="tab-divider"></div>
                    <button className="clear-search-btn" onClick={() => setChatSearch("")} aria-label="Clear search">
                      <i className="fas fa-times"></i>
                    </button>
                  </div>
                )}
              </div>
              <ThemeToggle />
              <button
                className="header-comparison-btn"
                onClick={() => setShowDiffCompare(true)}
                title="Compare different policy versions"
              >
                🔀 Comparison
              </button>
              <button className="header-new-chat-btn" onClick={createNewChat}>
                + New Chat
              </button>
            </div>
          </header>


          {showPolicies && (
            <div className="policy-popup">
              <div className="policy-popup-header">
                <b>Available Policies ({policies.length})</b>
                <button onClick={() => setShowPolicies(false)} className="close-popup">×</button>
              </div>

              {policies.length === 0 ? (
                <p>No policies uploaded</p>
              ) : (
                <ul className="policy-list">
                  {policies.map((p, i) => (
                    <li
                      key={i}
                      className="policy-item clickable"
                      onClick={() => handlePolicyClick(p)}
                    >
                      <span className="policy-name">📄 {p.name || p}</span>
                      <span className="policy-badge">{p.pages || '?'} pages</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}


          <div className="main-content">
            <div className="conversation-window" ref={chatWindowRef}>

              {conversation.length === 0 ? (
                <div className="empty-state">
                  <div className="icon-circle">
                    <img
                      src="/plogo.png"
                      alt="SRM University"
                      style={{
                        width: '80px',
                        height: '80px',
                        objectFit: 'contain',
                        borderRadius: '80%',
                        padding: '2px'
                      }}
                    />
                  </div>
                  <span className="pp-eyebrow">SRM University-AP · Policy Assistant</span>
                  <h3>Ask anything about university policies</h3>
                  <p>Answers come straight from the official policy documents, with the exact pages as evidence.</p>

                  <div className="suggestions-grid">
                    {suggestions.map((s, i) => (
                      <button
                        key={i}
                        className="suggestion-chip"
                        onClick={() => handleSuggestionClick(s)}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="messages-container">
                  {conversation.map((msg, i) => (
                    <div key={i} className={`message-bubble ${msg.type}-bubble ${msg.isNew ? 'new' : ''} ${msg.isNew && msg.type === "user" ? 'genie-new' : ''}`}>
                      <div className="message-header">
                        <span className={`message-avatar ${msg.type}-avatar`}>
                          {msg.type === "user" ? "Y" : "P"}
                        </span>
                        <div className="message-info">
                          <span className="message-sender">
                            {msg.type === "user" ? "You" : "NormBridge AI"}
                          </span>
                          <span className="message-time">{msg.time}</span>
                        </div>
                      </div>

                      <div className="message-content">
                        {msg.type === "bot" && msg.isNew ? (
                          <Typewriter
                            text={msg.text}
                            onUpdate={scrollToBottom}
                            formatFn={(txt) => formatText(txt, [], chatSearch)}
                            onComplete={() => {

                              const updated = [...conversation];
                              if (updated[i]) updated[i].isNew = false;
                              updateSessionMessages(currentSessionId, updated);
                              scrollToBottom();
                            }}
                          />
                        ) : (
                          formatText(msg.text, msg.sources || [])
                        )}
                      </div>

                      {msg.type === "bot" && !msg.isNew && msg.rewritten && (
                        <div className="pp-rewritten" title="Follow-up rewritten into a standalone question before searching the policies">
                          {msg.rewriteMethod === "fallback" ? "Used context from your previous question" : <>Searched as: “{msg.rewritten}”</>}
                        </div>
                      )}

                      {msg.type === "bot" && !msg.isNew && (
                        <div className="pp-answer-bar">
                          <GroundingBadge evaluation={msg.evaluation} />
                          {msg.cached && <span className="pp-chip-cached" title="Served from the semantic answer cache">⚡ Instant</span>}
                          {msg.fetchTime && <span className="pp-chip-time" title="Response time">{msg.fetchTime}s</span>}
                          <span className="pp-bar-spacer" />
                          <button type="button" className={`pp-icon-btn ${copiedIndex === i ? "done" : ""}`} onClick={() => copyAnswer(msg.text, i)} title={copiedIndex === i ? "Copied" : "Copy answer"} aria-label="Copy answer">
                            {copiedIndex === i ? (
                              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                            ) : (
                              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
                            )}
                          </button>
                          {msg.sources && msg.sources.length > 0 && (
                            <button type="button" className={`pp-icon-btn pp-icon-text ${expandedEvidence[i] ? "active" : ""}`} onClick={() => setExpandedEvidence(prev => ({ ...prev, [i]: !prev[i] }))} title="Show the policy pages this answer came from">
                              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8" /></svg>
                              <span>{expandedEvidence[i] ? "Hide sources" : `Sources (${msg.sources.length})`}</span>
                            </button>
                          )}
                          <button type="button" className={`pp-icon-btn ${speakingIndex === i ? "active" : ""}`} onClick={() => handleSpeak(msg.text, i)} title={speakingIndex === i ? "Stop reading" : "Read aloud"} aria-label="Read aloud">
                            {speakingIndex === i ? (
                              <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
                            ) : (
                              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5z" /><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14" /></svg>
                            )}
                          </button>
                          {msg.chatId && (
                            <>
                              <button type="button" className={`pp-icon-btn ${msg.satisfactionGiven === true ? "active" : ""}`} onClick={() => handleSatisfaction(msg.chatId, true, i)} disabled={msg.satisfactionGiven !== undefined} title="Helpful" aria-label="Helpful">
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M7 10v12M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" /></svg>
                              </button>
                              <button type="button" className={`pp-icon-btn ${msg.satisfactionGiven === false ? "active bad" : ""}`} onClick={() => handleSatisfaction(msg.chatId, false, i)} disabled={msg.satisfactionGiven !== undefined} title="Not helpful" aria-label="Not helpful">
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 14V2M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z" /></svg>
                              </button>
                              <button type="button" className="pp-icon-btn" onClick={() => handleReportInaccuracy(i)} title="Report an inaccurate answer" aria-label="Report inaccuracy">
                                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" /><path d="M4 22v-7" /></svg>
                              </button>
                            </>
                          )}
                        </div>
                      )}

                      {msg.type === "bot" && !msg.isNew && expandedEvidence[i] && msg.sources && msg.sources.length > 0 && (
                        <div className="evidence-panel page-entrance">
                          <div className="evidence-header">
                            <span>Policy pages behind this answer</span>
                          </div>
                          <div className="evidence-list">
                            {msg.sources.map((source, sIdx) => (
                              <div
                                key={sIdx}
                                className="evidence-item clickable-evidence"
                                onClick={() => handlePolicyClick({
                                  name: source.pdf,
                                  highlightText: source.text_snippet || source.text,
                                  page: source.page
                                })}
                                title={`Click to open ${source.pdf} at page ${source.page + 1}`}
                              >
                                <div className="evidence-meta">
                                  <span className="evidence-index">[{sIdx + 1}]</span>
                                  <span className="evidence-source link-style">{source.pdf} - Page {source.page + 1}</span>
                                </div>
                                <p className="evidence-text">
                                  "{formatText(source.text_snippet || source.text || "")}"
                                </p>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ))}

                  {(loading || isTyping) && !liveText && <ThinkingIndicator policyCount={policies.length} stage={liveStage} />}
                  {liveText && (
                    <div className="message-bubble bot-bubble pp-live" aria-live="polite">
                      <div className="message-header">
                        <span className="message-avatar bot-avatar">P</span>
                        <div className="message-info">
                          <span className="message-sender">NormBridge AI</span>
                          {liveStage && liveStage.model && <span className="message-time">streaming · {liveStage.model}</span>}
                        </div>
                      </div>
                      <div className="message-content">
                        {formatText(liveText, [])}
                        <span className="pp-stream-caret" />
                      </div>
                    </div>
                  )}

                  <div ref={messagesEndRef} />
                </div>
              )}
            </div>


            {showScrollDown && (
              <button type="button" className="pp-scroll-down" onClick={(e) => { e.preventDefault(); scrollToBottom(); }} title="Jump to latest" aria-label="Jump to latest message">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M19 12l-7 7-7-7" /></svg>
              </button>
            )}

            <div className="input-section">
              {error && <div className="error-message">{error}</div>}

              <div className="input-wrapper pp-composer">
                <button
                  className={`mic-btn pp-composer-btn ${isListening ? "is-listening" : ""}`}
                  onClick={toggleListening}
                  title={isListening ? "Stop voice input" : "Voice input"}
                  type="button"
                  aria-label="Voice input"
                >
                  {isListening ? (
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
                  ) : (
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4" /></svg>
                  )}
                </button>

                <div className="pp-model-menu-wrap" ref={modelMenuRef}>
                  <button
                    type="button"
                    className={`pp-composer-btn pp-model-btn ${showModelMenu ? "open" : ""}`}
                    onClick={() => setShowModelMenu(v => !v)}
                    title={`Model: ${selectedModel}`}
                    aria-haspopup="menu"
                    aria-expanded={showModelMenu}
                  >
                    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" /><path d="M19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z" /></svg>
                  </button>
                  {showModelMenu && (
                    <div className="pp-model-menu" role="menu">
                      <div className="pp-model-menu-title">Answer model</div>
                      {chatModels.map(m => (
                        <button
                          key={m}
                          type="button"
                          role="menuitemradio"
                          aria-checked={selectedModel === m}
                          className={`pp-model-option ${selectedModel === m ? "active" : ""}`}
                          onClick={() => { setSelectedModel(m); setShowModelMenu(false); inputRef.current && inputRef.current.focus(); }}
                        >
                          <span className="pp-model-name">{m}{m === defaultModel ? " · default" : ""}</span>
                          <span className="pp-model-meta">{modelStats[m] && modelStats[m].avg > 0 ? `${modelStats[m].avg}s avg` : "local"}</span>
                          {selectedModel === m && <span className="pp-model-check">✓</span>}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                <textarea
                  ref={inputRef}
                  value={q}
                  onChange={e => setQ(e.target.value)}
                  onKeyDown={handleKeyPress}
                  placeholder={isListening ? "Listening..." : "Ask about attendance, hostel, fees, exams…"}
                  className="chat-input"
                  disabled={loading}
                  rows="1"
                />

                <button
                  id="send-btn"
                  onClick={send}
                  className="send-btn"
                  disabled={loading || !q.trim()}
                  aria-label="Send"
                  title="Send (Enter)"
                >
                  {loading ? (
                    <div className="spinner-small"></div>
                  ) : (
                    <svg className="pp-send-icon" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M12 19V5M5 12l7-7 7 7" />
                    </svg>
                  )}
                </button>
              </div>
              <div className="input-hints">
                Answers come from official SRM AP policy documents · check the sources for important decisions
              </div>
            </div>
          </div>




          {showFeedback && isAuthenticated && createPortal(
            <div className="feedback-widget pp-feedback" role="dialog" aria-label="Rate your experience">
              {!feedbackSubmitted ? (
                <>
                  <div className="pp-feedback-head">
                    <h4>How was NormBridge AI?</h4>
                    <button className="pp-feedback-close" onClick={() => setShowFeedback(false)} aria-label="Close">×</button>
                  </div>
                  <div className="pp-feedback-stars">
                    {[1, 2, 3, 4, 5].map(star => (
                      <button
                        key={star}
                        type="button"
                        aria-label={`${star} star${star > 1 ? 's' : ''}`}
                        className={star <= feedbackStars ? 'on' : ''}
                        onClick={() => setFeedbackStars(star)}
                      >★</button>
                    ))}
                  </div>
                  <textarea
                    className="pp-feedback-text"
                    value={feedbackText}
                    onChange={(e) => setFeedbackText(e.target.value)}
                    placeholder="Tell us what you think..."
                  />
                  <button className="pp-feedback-submit" onClick={submitFeedback} disabled={feedbackStars === 0}>
                    Submit
                  </button>
                </>
              ) : (
                <div className="pp-feedback-thanks">
                  <div>🙏</div>
                  <h4>Thank you for your feedback!</h4>
                </div>
              )}
            </div>,
            document.body
          )}

        </div>
      </div>


      {showLogoutBye && (
        <div className="logout-bye-overlay">
          <div className="logout-bye-modal">
            <div className="logout-bye-icon">😊</div>
            <h3>Hope you had a great experience with NormBridge AI</h3>
            <p>Thank you for using our intelligent policy assistance system.</p>
            <button className="confirm-logout-btn" onClick={confirmLogout}>
              Logout
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default ChatApp;
