import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";

interface FooterNotesProps {
  footnotes: string[];
}

export function FooterNotes({ footnotes }: FooterNotesProps) {
  if (!footnotes.length) return null;

  return (
    <Box sx={{ mt: 2 }}>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        {footnotes.map((note, i) => (
          <li key={i}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {note}
            </Text>
          </li>
        ))}
      </ul>
    </Box>
  );
}
