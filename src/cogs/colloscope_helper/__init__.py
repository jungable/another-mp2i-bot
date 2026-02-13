from __future__ import annotations

import csv
import io
import logging
import os
import re
from functools import partial
from glob import glob
from typing import TYPE_CHECKING, Literal, cast

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from pdf2image.pdf2image import convert_from_bytes

from core.utils import BraceMessage as __

from . import colloscope_maker as cm

if TYPE_CHECKING:
    from bot import MP2IBot


logger = logging.getLogger(__name__)


class PlanningHelper(
    commands.GroupCog, group_name="colloscope", group_description="Un utilitaire pour gérer le colloscope"
):
    def __init__(self, bot: MP2IBot):
        self.bot = bot
        self.colloscopes: dict[str, cm.Colloscope]

        self.download_colloscope()
        self.load_colloscope()

        decorator = partial(
            app_commands.choices, class_=[app_commands.Choice(name=k, value=k) for k in self.colloscopes]
        )
        decorator()(self.quicklook)
        decorator()(self.export)
        decorator()(self.next_colle)

    def download_colloscope(self):
        url = os.environ.get("COLLOSCOPE_URL")
        if not url:
            logger.warning("COLLOSCOPE_URL not found in environment variables. Skipping download.")
            return

        try:
            logger.info(f"Downloading colloscope from {url}...")
            response = httpx.get(url, follow_redirects=True)
            response.raise_for_status()
            
            # Save raw data to ensure we have it (optional, but good for debug)
            # with open("./external_data/colloscopes/raw_data.csv", "wb") as f:
            #     f.write(response.content)

            content = response.content.decode("utf-8")
            transformed_lines = self.transform_mpi(content)

            output_path = "./external_data/colloscopes/mpi.csv"
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            with open(output_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter=",")
                writer.writerows(transformed_lines)
                
            logger.info(f"Colloscope downloaded and transformed successfully to {output_path}")

        except Exception as e:
            logger.error(f"Failed to download/transform colloscope: {e}")

    def transform_mpi(self, content: str) -> list[list[str]]:
        f = io.StringIO(content)
        reader = csv.reader(f, delimiter=",")
        lines = list(reader)

        if len(lines) < 4:
            return []

        header_line_index = 3 
        
        lines = lines[header_line_index:] 
        header = lines[0]
        lines = lines[2:]

        new_header = [""] * 5
        valid_indices = []
        for i, column in enumerate(header):
            if i < 5:
                continue
            if not column: continue
            
            date_parts = column.split("-")
            if len(date_parts) == 3:
                 # DD-MM-YYYY -> DD/MM/YY
                 year = date_parts[2]
                 if len(year) == 4: year = year[2:]
                 new_header.append(f"{date_parts[0]}/{date_parts[1]}/{year}")
                 valid_indices.append(i)
            else:
                 new_header.append(column)
                 valid_indices.append(i)

        processed_lines = []
        # The header included for stats
        final_header = ["Matiere", "Prof", "Jour", "Heure", "Salle"] + new_header[5:]
        processed_lines.append(final_header)

        for line in lines:
            if not line or not any(line): continue
            if len(line) < 5: continue
            
            day = line[3]
            hour = line[4]
            room = line[2]
            
            hour = hour.split("-")[0].replace(" ", "")

            new_row = [line[0], line[1], day, hour, room]
            
            # Append only the columns corresponding to valid dates/headers
            for index in valid_indices:
                if index < len(line):
                    val = line[index]
                    match = re.search(r"(\d+)", val)
                    if match:
                        new_row.append(match.group(1))
                    else:
                         new_row.append("")
                else:
                    new_row.append("") 

            processed_lines.append(new_row)
        
        return processed_lines

    def load_colloscope(self):
        self.colloscopes = {}
        for csv_file in glob("./external_data/colloscopes/*.csv"):
            class_ = os.path.splitext(os.path.basename(csv_file))[0]
            if class_ == "example":
                continue
            try:
                self.colloscopes[class_.lower()] = cm.Colloscope.from_filename(csv_file)
                logger.info(f"Loaded colloscope for class {class_}")
            except Exception as e:
                logger.warning(
                    __("Error while reading the colloscope from : {filename}", filename=csv_file), exc_info=e
                )

    @app_commands.command(name="aperçu", description="Affiche l'aperçu du colloscope")
    @app_commands.rename(class_="classe", group="groupe")
    @app_commands.describe(class_="Votre classe.", group="Votre groupe de colle.")
    async def quicklook(self, inter: discord.Interaction, class_: str, group: str):
        class_key = class_.lower()
        if class_key not in self.colloscopes:
            available = ", ".join(self.colloscopes.keys())
            await inter.response.send_message(
                f"❌ Classe '{class_}' introuvable. Classes disponibles : {available}",
                ephemeral=True
            )
            return

        # Defer immediately to avoid timeout/active waiting feel
        await inter.response.defer()

        colloscope = self.colloscopes[class_key]
        colles = cm.sort_colles(colloscope.colles, sort_type="temps")

        filtered_colles = [c for c in colles if c.group == str(group)]
        if not filtered_colles:
            await inter.followup.send(f"Aucune colle trouvée pour le groupe {group}")
            return

        loop = self.bot.loop
        files = await loop.run_in_executor(None, self._generate_quicklook_files, filtered_colles, group, colloscope)

        await inter.followup.send(files=files)

    def _generate_quicklook_files(self, filtered_colles, group, colloscope):
        buffer = io.BytesIO()
        cm.write_colles(buffer, "pdf", filtered_colles, str(group), colloscope.holidays)
        buffer.seek(0)
        
        # Convert PDF to images
        images = convert_from_bytes(buffer.read())

        files: list[discord.File] = []
        for i, img in enumerate(images):
            img_buffer = io.BytesIO()
            img.save(img_buffer, format="png")
            img_buffer.seek(0)
            files.append(discord.File(img_buffer, f"{i}.png"))
        return files

    @app_commands.command(name="export", description="Exporte le colloscope dans un fichier")
    @app_commands.rename(class_="classe", group="groupe")
    @app_commands.describe(
        class_="Votre classe", group="Votre groupe de colle", format="Le format du fichier à exporter"
    )
    async def export(
        self,
        inter: discord.Interaction,
        class_: str,
        group: str,
        format: Literal["pdf", "csv", "agenda", "todoist"] = "pdf",
    ):
        class_key = class_.lower()
        if class_key not in self.colloscopes:
            available = ", ".join(self.colloscopes.keys())
            await inter.response.send_message(
                f"Classe '{class_}' introuvable. Classes disponibles : {available}",
                ephemeral=True
            )
            return

        colloscope = self.colloscopes[class_key]

        colles = cm.sort_colles(colloscope.colles, sort_type="temps")  # sort by time
        filtered_colles = [c for c in colles if c.group == str(group)]
        if not filtered_colles:
            raise ValueError("Aucune colle n'a été trouvé pour ce groupe")

        if format in ["agenda", "csv", "todoist"]:
            format = cast(Literal["agenda", "csv", "todoist"], format)
            buffer = io.StringIO()
            cm.write_colles(buffer, format, filtered_colles, str(group), colloscope.holidays)
            buffer = io.BytesIO(buffer.getvalue().encode())
            format = "csv"
        else:
            format = cast(Literal["pdf"], format)
            buffer = io.BytesIO()
            cm.write_colles(buffer, format, filtered_colles, str(group), colloscope.holidays)
        buffer.seek(0)
        file = discord.File(buffer, filename=f"colloscope.{format}")
        await inter.response.send_message(file=file)

    @app_commands.command(name="prochaine_colle", description="Affiche la prochaine colle")
    @app_commands.rename(class_="classe", group="groupe", nb="nombre")
    @app_commands.describe(class_="Votre classe.", group="Votre groupe de colle.", nb="Le nombre de colle à afficher.")
    async def next_colle(self, inter: discord.Interaction, class_: str, group: str, nb: int = 5):
        class_key = class_.lower()
        if class_key not in self.colloscopes:
            available = ", ".join(self.colloscopes.keys())
            await inter.response.send_message(
                f"Classe '{class_}' introuvable. Classes disponibles : {available}",
                ephemeral=True
            )
            return

        colloscope = self.colloscopes[class_key]

        sorted_colles = cm.get_group_upcoming_colles(colloscope.colles, str(group))
        
        if not sorted_colles:
            await inter.response.send_message(f"Aucune colle trouvée pour le groupe {group}", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Prochaines Colles - Groupe {group}",
            description=f"Voici les {min(nb, len(sorted_colles), 12)} prochaines colles :",
            color=discord.Color.from_rgb(58, 134, 255) # A nice blue
        )
        
        # Add footer with requestor info
        embed.set_footer(text=f"Demandé par {inter.user.display_name}")

        for i in range(min(nb, len(sorted_colles), 12)):
            colle = sorted_colles[i]
            date_str = colle.long_str_date.title()
            
            # Create a nice field for each colle
            embed.add_field(
                name=f"{date_str} - {colle.str_time}",
                value=f"**{colle.subject}**\n{colle.professor}\nSalle {colle.classroom}",
                inline=False
            )
            
        await inter.response.send_message(embed=embed)

    @next_colle.autocomplete("group")
    @export.autocomplete("group")
    @quicklook.autocomplete("group")
    async def group_autocompleter(self, inter: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if inter.namespace.classe is None:
            return [app_commands.Choice(name="Sélectionnez une classe avant un groupe", value="-1")]

        class_key = inter.namespace.classe.lower()
        if class_key not in self.colloscopes:
             return []

        groups = sorted(self.colloscopes[class_key].groups)
        return [
            app_commands.Choice(name=g, value=g)
            for g in groups
            if g.startswith(current)
        ][:25] # Limit to 25 choices to avoid discord errors


async def setup(bot: MP2IBot):
    await bot.add_cog(PlanningHelper(bot))
