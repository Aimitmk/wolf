import Link from "next/link";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Container from "@mui/material/Container";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import GamesTable, { EmptyGamesState } from "@/components/GamesTable";
import { listGames } from "@/lib/data";

export default async function GamesListPage() {
  const games = await listGames();
  return (
    <Container maxWidth="xl" sx={{ py: 3 }}>
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        sx={{ mb: 2 }}
      >
        <Box>
          <Typography variant="h5" sx={{ fontWeight: 600 }}>
            Wolfbot Game Viewer
          </Typography>
          <Typography variant="body2" color="text.secondary">
            終了したゲームの一覧。行をクリックすると詳細ビューに移動します。
          </Typography>
        </Box>
        <Button
          component={Link}
          href="/sample"
          variant="outlined"
          size="small"
        >
          サンプルゲームを開く
        </Button>
      </Stack>
      {games.length === 0 ? <EmptyGamesState /> : <GamesTable games={games} />}
    </Container>
  );
}
