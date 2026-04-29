import Link from "next/link";
import Container from "@mui/material/Container";
import Paper from "@mui/material/Paper";
import Typography from "@mui/material/Typography";

export default function NotFound() {
  return (
    <Container maxWidth="md" sx={{ py: 6 }}>
      <Paper variant="outlined" sx={{ p: 4, textAlign: "center" }}>
        <Typography variant="h6" sx={{ mb: 1 }}>
          ゲームが見つかりません
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          指定された <code>game_id</code> に対応する JSON が{" "}
          <code>viewer/games/</code> に存在しません。
        </Typography>
        <Link href="/" style={{ textDecoration: "underline", color: "inherit" }}>
          ゲーム一覧に戻る
        </Link>
      </Paper>
    </Container>
  );
}
