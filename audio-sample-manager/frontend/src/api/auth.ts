import { api } from "./client";
import type { Token, UserOut } from "../types";

export async function login(username: string, password: string): Promise<Token> {
  const form = new URLSearchParams();
  form.append("username", username);
  form.append("password", password);
  const res = await api.post<Token>("/auth/token", form, {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  return res.data;
}

export async function register(
  email: string,
  username: string,
  password: string
): Promise<UserOut> {
  const res = await api.post<UserOut>("/auth/register", { email, username, password });
  return res.data;
}
