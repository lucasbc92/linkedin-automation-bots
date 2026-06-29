import unicodedata


def _normalize_name_token(token):
    """Lowercase and strip accents so 'Júlia' and 'Julia' compare equal."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', token.lower())
        if unicodedata.category(c) != 'Mn')


# Connectors that can appear inside a compound first name (e.g. "Maria de Lourdes")
NAME_CONNECTORS = {"de", "da", "do", "das", "dos", "e"}

# Common Brazilian given names that frequently appear as the SECOND part of a
# compound first name. If the second token is in this set we keep it
# ("João Victor"); a surname (Silva, Santos) is not, so it is dropped.
COMPOUND_GIVEN_NAMES = {
    _normalize_name_token(n) for n in [
        # masculine
        "Victor", "Vitor", "Henrique", "Eduardo", "Gabriel", "Felipe", "Miguel",
        "Lucas", "Luiz", "Luis", "Carlos", "Paulo", "Pedro", "Antonio", "Jose",
        "Augusto", "Cesar", "Otavio", "Vinicius", "Heitor", "Davi", "David",
        "Bernardo", "Arthur", "Artur", "Theo", "Benicio", "Samuel", "Isaac",
        "Gustavo", "Mateus", "Matheus", "Joao", "Ricardo", "Rodrigo", "Marcelo",
        "Marcos", "Marco", "Andre", "Alexandre", "Leonardo", "Leandro", "Fabio",
        "Bruno", "Diego", "Thiago", "Tiago", "Rafael", "Sergio", "Emanuel",
        "Enzo", "Lorenzo", "Murilo", "Nicolas", "Breno", "Caio", "Kaua", "Kauan",
        "Francisco", "Vicente", "Daniel", "Fernando", "Junior", "Filho", "Neto",
        # feminine
        "Julia", "Eduarda", "Gabriela", "Clara", "Helena", "Beatriz", "Fernanda",
        "Cristina", "Lucia", "Vitoria", "Sofia", "Sophia", "Manuela", "Alice",
        "Laura", "Leticia", "Rafaela", "Daniela", "Carolina", "Luiza", "Luisa",
        "Mariana", "Marina", "Marcela", "Juliana", "Adriana", "Patricia",
        "Priscila", "Vanessa", "Viviane", "Simone", "Sandra", "Debora", "Deborah",
        "Raquel", "Sara", "Sarah", "Tatiana", "Michele", "Michelle", "Isabel",
        "Isabela", "Isabella", "Valentina", "Cecilia", "Antonella", "Elisa",
        "Livia", "Melissa", "Yasmin", "Agatha", "Esther", "Ester", "Lais", "Lara",
        "Lourdes", "Aparecida", "Conceicao", "Regina", "Teresa", "Tereza", "Rosa",
        "Ana", "Maria", "Emanuelly", "Gabrielly", "Vitoria", "Flavia", "Camila",
        "Bianca", "Larissa", "Amanda", "Aline", "Bruna", "Carla", "Paula",
    ]
}


def display_first_name(full_name):
    """Return the name to use in the {name} placeholder.

    Usually just the first token, but keeps compound first names ("João Victor",
    "Ana Júlia", "Maria de Lourdes"): the extra token is included only when it
    is itself a known given name, so surnames (Silva, Santos) are dropped.
    """
    if not full_name:
        return ""
    tokens = full_name.split()
    if not tokens:
        return ""
    first = tokens[0]
    if len(tokens) == 1:
        return first

    # "Maria de Lourdes" -> keep connector + following given name
    if _normalize_name_token(tokens[1]) in NAME_CONNECTORS and len(tokens) >= 3:
        if _normalize_name_token(tokens[2]) in COMPOUND_GIVEN_NAMES:
            return f"{first} {tokens[1]} {tokens[2]}"
        return first

    # "João Victor" -> keep second token only if it's a given name
    if _normalize_name_token(tokens[1]) in COMPOUND_GIVEN_NAMES:
        return f"{first} {tokens[1]}"

    return first
