//! A tolerant HTML tokenizer that reads a page the way Python's
//! `html.parser.HTMLParser` (`convert_charrefs=True`) does for the few things the
//! Letterboxd list harvest looks at: start tags with their attributes, end tags, and the
//! text between them.
//!
//! Comments, declarations and processing instructions produce nothing. The content of a
//! `<script>` or `<style>` element is raw text up to its closing tag, so a tag-shaped
//! string inside a script never reads as a tag.
//!
//! Character references are decoded for numeric forms (with Python's replacement of the
//! C1 control range by Windows-1252) and for the named ones in [`NAMED`], which covers
//! the Latin-1 set and the punctuation a page title uses. A name outside that table is
//! left as written, where Python would decode it.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Token {
    Start {
        tag: String,
        attrs: Vec<(String, Option<String>)>,
        self_closing: bool,
    },
    End(String),
    Data(String),
}

/// An attribute's value by name; a later attribute of the same name wins, as in
/// `dict(attrs)`. The outer `Option` is "no such attribute", the inner is an attribute
/// written with no value.
pub fn attr<'a>(attrs: &'a [(String, Option<String>)], name: &str) -> Option<&'a Option<String>> {
    attrs
        .iter()
        .rev()
        .find(|(attr_name, _)| attr_name == name)
        .map(|(_, value)| value)
}

/// The attribute's text, or `None` when it is absent or has no value.
pub fn attr_text<'a>(attrs: &'a [(String, Option<String>)], name: &str) -> Option<&'a str> {
    attr(attrs, name).and_then(|value| value.as_deref())
}

fn is_space(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0c)
}

pub fn tokenize(html: &str) -> Vec<Token> {
    let bytes = html.as_bytes();
    let mut tokens = Vec::new();
    let mut text = String::new();
    let mut i = 0;
    let flush = |text: &mut String, tokens: &mut Vec<Token>| {
        if !text.is_empty() {
            tokens.push(Token::Data(unescape(text)));
            text.clear();
        }
    };
    while i < bytes.len() {
        if bytes[i] != b'<' {
            let next = html[i..].find('<').map_or(bytes.len(), |n| i + n);
            text.push_str(&html[i..next]);
            i = next;
            continue;
        }
        let rest = &html[i..];
        if let Some(comment) = rest.strip_prefix("<!--") {
            match comment.find("-->") {
                Some(n) => {
                    flush(&mut text, &mut tokens);
                    i += 4 + n + 3;
                }
                None => {
                    text.push_str(rest);
                    i = bytes.len();
                }
            }
        } else if rest.starts_with("<!") || rest.starts_with("<?") {
            match rest.find('>') {
                Some(n) => {
                    flush(&mut text, &mut tokens);
                    i += n + 1;
                }
                None => {
                    text.push_str(rest);
                    i = bytes.len();
                }
            }
        } else if let Some(after) = rest.strip_prefix("</") {
            let Some(close) = after.find('>') else {
                text.push_str(rest);
                i = bytes.len();
                continue;
            };
            flush(&mut text, &mut tokens);
            let inside =
                after[..close].trim_start_matches(|c: char| is_space(c as u8) && c.is_ascii());
            if inside
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphabetic)
            {
                let end = inside
                    .find(|c: char| c.is_ascii() && (is_space(c as u8) || c == '/' || c == '>'))
                    .unwrap_or(inside.len());
                tokens.push(Token::End(inside[..end].to_ascii_lowercase()));
            }
            i += 2 + close + 1;
        } else if rest.as_bytes().get(1).is_some_and(u8::is_ascii_alphabetic) {
            match start_tag(rest) {
                Some((tag, attrs, self_closing, length)) => {
                    flush(&mut text, &mut tokens);
                    i += length;
                    let raw = matches!(tag.as_str(), "script" | "style");
                    tokens.push(Token::Start {
                        tag: tag.clone(),
                        attrs,
                        self_closing,
                    });
                    if self_closing {
                        tokens.push(Token::End(tag.clone()));
                    }
                    if raw && !self_closing {
                        i = raw_text(html, i, &tag, &mut tokens);
                    }
                }
                None => {
                    text.push_str(rest);
                    i = bytes.len();
                }
            }
        } else {
            text.push('<');
            i += 1;
        }
    }
    flush(&mut text, &mut tokens);
    tokens
}

/// Skip the raw content of a `<script>` or `<style>` from `from`, emitting it as
/// undecoded text and the closing tag; returns where to resume.
fn raw_text(html: &str, from: usize, tag: &str, tokens: &mut Vec<Token>) -> usize {
    let lower = html[from..].to_ascii_lowercase();
    let needle = format!("</{tag}");
    let mut search = 0;
    while let Some(found) = lower[search..].find(&needle) {
        let at = search + found;
        let after = &lower[at + needle.len()..];
        let boundary = after
            .bytes()
            .next()
            .is_none_or(|c| is_space(c) || c == b'>' || c == b'/');
        if boundary && let Some(close) = after.find('>') {
            if at > 0 {
                tokens.push(Token::Data(html[from..from + at].to_string()));
            }
            tokens.push(Token::End(tag.to_string()));
            return from + at + needle.len() + close + 1;
        }
        search = at + needle.len();
    }
    if from < html.len() {
        tokens.push(Token::Data(html[from..].to_string()));
    }
    html.len()
}

type StartTag = (String, Vec<(String, Option<String>)>, bool, usize);

/// One start tag at the head of `rest` (which begins `<` and a letter): its name, its
/// attributes, whether it closes itself, and its length; `None` when it never ends.
fn start_tag(rest: &str) -> Option<StartTag> {
    let bytes = rest.as_bytes();
    let mut i = 1;
    while i < bytes.len()
        && !is_space(bytes[i])
        && bytes[i] != b'/'
        && bytes[i] != b'>'
        && bytes[i] != 0
    {
        i += 1;
    }
    let tag = rest[1..i].to_ascii_lowercase();
    let mut attrs = Vec::new();
    let mut self_closing = false;
    loop {
        while i < bytes.len()
            && (is_space(bytes[i]) || (bytes[i] == b'/' && bytes.get(i + 1) != Some(&b'>')))
        {
            i += 1;
        }
        match bytes.get(i) {
            None => return None,
            Some(b'>') => {
                i += 1;
                break;
            }
            Some(b'/') => {
                self_closing = true;
                i += 2;
                break;
            }
            Some(_) => {}
        }
        let start = i;
        i += 1;
        while i < bytes.len() && !is_space(bytes[i]) && !matches!(bytes[i], b'/' | b'=' | b'>') {
            i += 1;
        }
        let name = rest[start..i].to_ascii_lowercase();
        let mut probe = i;
        while probe < bytes.len() && is_space(bytes[probe]) {
            probe += 1;
        }
        if bytes.get(probe) != Some(&b'=') {
            attrs.push((name, None));
            continue;
        }
        while probe < bytes.len() && bytes[probe] == b'=' {
            probe += 1;
        }
        while probe < bytes.len() && is_space(bytes[probe]) {
            probe += 1;
        }
        let value = match bytes.get(probe) {
            Some(&quote @ (b'\'' | b'"')) => {
                let close = rest[probe + 1..].find(quote as char)?;
                let value = &rest[probe + 1..probe + 1 + close];
                i = probe + 1 + close + 1;
                value
            }
            _ => {
                let from = probe;
                while probe < bytes.len() && !is_space(bytes[probe]) && bytes[probe] != b'>' {
                    probe += 1;
                }
                i = probe;
                &rest[from..probe]
            }
        };
        let value = if value.is_empty() {
            String::new()
        } else {
            unescape(value)
        };
        attrs.push((name, Some(value)));
    }
    Some((tag, attrs, self_closing, i))
}

const C1: [u32; 32] = [
    0x20ac, 0x81, 0x201a, 0x0192, 0x201e, 0x2026, 0x2020, 0x2021, 0x02c6, 0x2030, 0x0160, 0x2039,
    0x0152, 0x8d, 0x017d, 0x8f, 0x90, 0x2018, 0x2019, 0x201c, 0x201d, 0x2022, 0x2013, 0x2014,
    0x02dc, 0x2122, 0x0161, 0x203a, 0x0153, 0x9d, 0x017e, 0x0178,
];

/// Named references: the HTML 4 Latin-1 set, then punctuation and symbols.
const NAMED: &[(&str, char)] = &[
    ("amp", '&'),
    ("lt", '<'),
    ("gt", '>'),
    ("quot", '"'),
    ("apos", '\''),
    ("nbsp", '\u{a0}'),
    ("iexcl", '¡'),
    ("cent", '¢'),
    ("pound", '£'),
    ("curren", '¤'),
    ("yen", '¥'),
    ("brvbar", '¦'),
    ("sect", '§'),
    ("uml", '¨'),
    ("copy", '©'),
    ("ordf", 'ª'),
    ("laquo", '«'),
    ("not", '¬'),
    ("shy", '\u{ad}'),
    ("reg", '®'),
    ("macr", '¯'),
    ("deg", '°'),
    ("plusmn", '±'),
    ("sup2", '²'),
    ("sup3", '³'),
    ("acute", '´'),
    ("micro", 'µ'),
    ("para", '¶'),
    ("middot", '·'),
    ("cedil", '¸'),
    ("sup1", '¹'),
    ("ordm", 'º'),
    ("raquo", '»'),
    ("frac14", '¼'),
    ("frac12", '½'),
    ("frac34", '¾'),
    ("iquest", '¿'),
    ("Agrave", 'À'),
    ("Aacute", 'Á'),
    ("Acirc", 'Â'),
    ("Atilde", 'Ã'),
    ("Auml", 'Ä'),
    ("Aring", 'Å'),
    ("AElig", 'Æ'),
    ("Ccedil", 'Ç'),
    ("Egrave", 'È'),
    ("Eacute", 'É'),
    ("Ecirc", 'Ê'),
    ("Euml", 'Ë'),
    ("Igrave", 'Ì'),
    ("Iacute", 'Í'),
    ("Icirc", 'Î'),
    ("Iuml", 'Ï'),
    ("ETH", 'Ð'),
    ("Ntilde", 'Ñ'),
    ("Ograve", 'Ò'),
    ("Oacute", 'Ó'),
    ("Ocirc", 'Ô'),
    ("Otilde", 'Õ'),
    ("Ouml", 'Ö'),
    ("times", '×'),
    ("Oslash", 'Ø'),
    ("Ugrave", 'Ù'),
    ("Uacute", 'Ú'),
    ("Ucirc", 'Û'),
    ("Uuml", 'Ü'),
    ("Yacute", 'Ý'),
    ("THORN", 'Þ'),
    ("szlig", 'ß'),
    ("agrave", 'à'),
    ("aacute", 'á'),
    ("acirc", 'â'),
    ("atilde", 'ã'),
    ("auml", 'ä'),
    ("aring", 'å'),
    ("aelig", 'æ'),
    ("ccedil", 'ç'),
    ("egrave", 'è'),
    ("eacute", 'é'),
    ("ecirc", 'ê'),
    ("euml", 'ë'),
    ("igrave", 'ì'),
    ("iacute", 'í'),
    ("icirc", 'î'),
    ("iuml", 'ï'),
    ("eth", 'ð'),
    ("ntilde", 'ñ'),
    ("ograve", 'ò'),
    ("oacute", 'ó'),
    ("ocirc", 'ô'),
    ("otilde", 'õ'),
    ("ouml", 'ö'),
    ("divide", '÷'),
    ("oslash", 'ø'),
    ("ugrave", 'ù'),
    ("uacute", 'ú'),
    ("ucirc", 'û'),
    ("uuml", 'ü'),
    ("yacute", 'ý'),
    ("thorn", 'þ'),
    ("yuml", 'ÿ'),
    ("OElig", 'Œ'),
    ("oelig", 'œ'),
    ("Scaron", 'Š'),
    ("scaron", 'š'),
    ("Yuml", 'Ÿ'),
    ("fnof", 'ƒ'),
    ("circ", 'ˆ'),
    ("tilde", '˜'),
    ("ensp", '\u{2002}'),
    ("emsp", '\u{2003}'),
    ("thinsp", '\u{2009}'),
    ("zwnj", '\u{200c}'),
    ("zwj", '\u{200d}'),
    ("ndash", '–'),
    ("mdash", '—'),
    ("lsquo", '‘'),
    ("rsquo", '’'),
    ("sbquo", '‚'),
    ("ldquo", '“'),
    ("rdquo", '”'),
    ("bdquo", '„'),
    ("dagger", '†'),
    ("Dagger", '‡'),
    ("bull", '•'),
    ("hellip", '…'),
    ("permil", '‰'),
    ("prime", '′'),
    ("Prime", '″'),
    ("lsaquo", '‹'),
    ("rsaquo", '›'),
    ("euro", '€'),
    ("trade", '™'),
    ("larr", '←'),
    ("uarr", '↑'),
    ("rarr", '→'),
    ("darr", '↓'),
    ("hearts", '♥'),
    ("infin", '∞'),
    ("ne", '≠'),
    ("le", '≤'),
    ("ge", '≥'),
];

/// `html.unescape` for the references this module knows.
pub fn unescape(text: &str) -> String {
    if !text.contains('&') {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find('&') {
        out.push_str(&rest[..at]);
        rest = &rest[at..];
        match reference(rest) {
            Some((decoded, used)) => {
                out.push(decoded);
                rest = &rest[used..];
            }
            None => {
                out.push('&');
                rest = &rest[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

/// The character the reference at the head of `rest` (which begins `&`) names, and its
/// length in bytes.
fn reference(rest: &str) -> Option<(char, usize)> {
    let body = &rest[1..];
    if let Some(number) = body.strip_prefix('#') {
        let (digits, radix, skip) = match number.strip_prefix(['x', 'X']) {
            Some(hex) => (hex, 16, 3),
            None => (number, 10, 2),
        };
        let len = digits
            .bytes()
            .take_while(|b| (*b as char).is_digit(radix))
            .count();
        if len == 0 {
            return None;
        }
        let value = u32::from_str_radix(&digits[..len], radix).unwrap_or(u32::MAX);
        let used = skip + len + usize::from(digits[len..].starts_with(';'));
        let mapped = match value {
            0 => 0xfffd,
            0x80..=0x9f => C1[(value - 0x80) as usize],
            v if v > 0x10ffff || (0xd800..=0xdfff).contains(&v) => 0xfffd,
            v => v,
        };
        return Some((char::from_u32(mapped).unwrap_or('\u{fffd}'), used));
    }
    let len = body.bytes().take_while(u8::is_ascii_alphanumeric).count();
    if len == 0 || !body[len..].starts_with(';') {
        return None;
    }
    NAMED
        .iter()
        .find(|(name, _)| *name == &body[..len])
        .map(|(_, decoded)| (*decoded, 1 + len + 1))
}

#[cfg(test)]
mod tests {
    use super::*;

    type Starts = Vec<(String, Vec<(String, Option<String>)>)>;

    fn starts(html: &str) -> Starts {
        tokenize(html)
            .into_iter()
            .filter_map(|t| match t {
                Token::Start { tag, attrs, .. } => Some((tag, attrs)),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn attributes_are_lowercased_unquoted_and_decoded() {
        let found = starts(
            r#"<DIV Class='a b' data-x=1&amp;2 hidden DATA-Item-Name="Am&eacute;lie (2001)">"#,
        );
        assert_eq!(found[0].0, "div");
        assert_eq!(attr_text(&found[0].1, "class"), Some("a b"));
        assert_eq!(attr_text(&found[0].1, "data-x"), Some("1&2"));
        assert_eq!(attr(&found[0].1, "hidden"), Some(&None));
        assert_eq!(
            attr_text(&found[0].1, "data-item-name"),
            Some("Amélie (2001)")
        );
    }

    #[test]
    fn script_content_never_reads_as_tags() {
        let html = r#"<body><script>var a = "<div data-item-slug='x' data-item-name='X (2000)'>";</script><p>"#;
        let tags: Vec<String> = starts(html).into_iter().map(|(t, _)| t).collect();
        assert_eq!(tags, ["body", "script", "p"]);
    }

    #[test]
    fn comments_and_doctypes_yield_nothing_and_text_is_decoded() {
        let tokens = tokenize("<!DOCTYPE html><!-- <b> -->a &lt; b &#39;c&#x27; &bogus; &#0;");
        assert_eq!(tokens, [Token::Data("a < b 'c' &bogus; \u{fffd}".into())]);
    }

    #[test]
    fn a_self_closing_tag_starts_and_ends() {
        let tokens = tokenize("<br/>");
        assert!(matches!(
            &tokens[0],
            Token::Start {
                self_closing: true,
                ..
            }
        ));
        assert_eq!(tokens[1], Token::End("br".into()));
    }

    #[test]
    fn an_unfinished_tag_is_text() {
        assert_eq!(
            tokenize("a <div class="),
            [Token::Data("a <div class=".into())]
        );
    }
}
