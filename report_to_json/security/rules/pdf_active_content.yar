/*
  Active content in PDF documents.

  These rules run only against the PDF object dictionaries the gate extracts
  with PyMuPDF (external variable kind == "pdfobj"). That view is decompressed,
  so keywords hidden inside object streams are seen, and it excludes page
  content and compressed stream bytes, where "/JS" can occur by chance.
  Name obfuscation with #xx hex escapes (e.g. /J#61vaScript) is matched.

  meta.verdict: "malicious" | "suspicious"  (drives the gate's decision)
  Embedded file attachments are handled by the gate itself, which extracts
  and classifies each one.
*/

rule PDF_Launch_Action
{
    meta:
        verdict = "malicious"
        description = "PDF /Launch action can start external programs"
    strings:
        $launch = /\/(L|#4[cC])(a|#61)(u|#75)(n|#6[eE])(c|#63)(h|#68)\b/
    condition:
        kind == "pdfobj" and $launch
}

rule PDF_AutoRun_JavaScript
{
    meta:
        verdict = "malicious"
        description = "JavaScript that runs automatically on open (/OpenAction or /AA with /JS)"
    strings:
        $js = /\/(J|#4[aA])((a|#61)(v|#76)(a|#61)(S|#53)(c|#63)(r|#72)(i|#69)(p|#70)(t|#74)|(S|#53))\b/
        $open = /\/(O|#4[fF])(p|#70)(e|#65)(n|#6[eE])(A|#41)(c|#63)(t|#74)(i|#69)(o|#6[fF])(n|#6[eE])/
        $aa = /\/(A|#41)(A|#41)\s*<</
    condition:
        kind == "pdfobj" and $js and ($open or $aa)
}

rule PDF_JavaScript
{
    meta:
        verdict = "suspicious"
        description = "PDF contains JavaScript"
    strings:
        $js = /\/(J|#4[aA])((a|#61)(v|#76)(a|#61)(S|#53)(c|#63)(r|#72)(i|#69)(p|#70)(t|#74)|(S|#53))\b/
    condition:
        kind == "pdfobj" and $js
}


rule PDF_Rich_Media_Or_XFA
{
    meta:
        verdict = "suspicious"
        description = "PDF uses RichMedia (Flash) or XFA forms, common exploit carriers"
    strings:
        $rich = "/RichMedia"
        $xfa = "/XFA"
    condition:
        kind == "pdfobj" and any of them
}
