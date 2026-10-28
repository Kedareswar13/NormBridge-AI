// Signed session token issued by the API at login. Identity-dependent endpoints read the user from this token,
// never from an email the browser sends.
const TOKEN_KEY = "ppToken";

export const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
export const saveToken = (token) => { if (token) localStorage.setItem(TOKEN_KEY, token); };
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);
export const authHeaders = () => {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
};
