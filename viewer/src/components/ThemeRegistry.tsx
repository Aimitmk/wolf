"use client";

import { ThemeProvider, createTheme } from "@mui/material/styles";

const theme = createTheme({
  palette: {
    mode: "light",
    primary: { main: "#3f51b5" },
    secondary: { main: "#9c27b0" },
    background: { default: "#fafafa", paper: "#ffffff" },
  },
  typography: {
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic UI", "Segoe UI", Roboto, sans-serif',
    fontSize: 13,
  },
  shape: { borderRadius: 6 },
});

export default function ThemeRegistry({
  children,
}: {
  children: React.ReactNode;
}) {
  return <ThemeProvider theme={theme}>{children}</ThemeProvider>;
}
