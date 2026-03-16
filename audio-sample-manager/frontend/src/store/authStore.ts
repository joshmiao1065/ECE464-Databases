import { create } from "zustand";
import { login as apiLogin, register as apiRegister } from "../api/auth";

interface AuthState {
  token: string | null;
  username: string | null;
  login: (username: string, password: string) => Promise<void>;
  register: (email: string, username: string, password: string) => Promise<void>;
  logout: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: localStorage.getItem("access_token"),
  username: localStorage.getItem("username"),

  login: async (username, password) => {
    const data = await apiLogin(username, password);
    localStorage.setItem("access_token", data.access_token);
    localStorage.setItem("username", username);
    set({ token: data.access_token, username });
  },

  register: async (email, username, password) => {
    await apiRegister(email, username, password);
    // Auto-login after registration
    const data = await apiLogin(username, password);
    localStorage.setItem("access_token", data.access_token);
    localStorage.setItem("username", username);
    set({ token: data.access_token, username });
  },

  logout: () => {
    localStorage.removeItem("access_token");
    localStorage.removeItem("username");
    set({ token: null, username: null });
  },
}));
