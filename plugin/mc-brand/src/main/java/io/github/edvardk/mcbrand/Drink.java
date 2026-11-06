package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import io.papermc.paper.datacomponent.DataComponentTypes;
import io.papermc.paper.datacomponent.item.TooltipDisplay;
import net.kyori.adventure.text.Component;
import org.bukkit.Color;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Player;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.PlayerInventory;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.inventory.meta.PotionMeta;
import org.bukkit.inventory.meta.components.CustomModelDataComponent;
import org.bukkit.inventory.meta.components.UseCooldownComponent;
import org.bukkit.persistence.PersistentDataContainer;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.potion.PotionEffect;
import org.bukkit.potion.PotionEffectType;

/**
 * The server's signature drink (branding.drink: its name, lore, colour and command, /drink by default): a
 * potion that never empties. Cosmetic only (verified against purpur-api 26.3 build
 * 2655): a POTION without potion contents (no effects), a custom colour, name and lore, one per stack; drinking
 * it (PlayerItemConsumeEvent) puts a fresh copy back into the hand through {@code setReplacement} (applied by
 * LivingEntity.completeUsingItem after finishUsingItem, so no glass bottle is left) and gives a short Nausea.
 * The use_cooldown component (its own cooldown group, so other potions are unaffected) makes the client show the
 * cooldown and the server refuse a drink during it; the plugin checks its own clock too.
 *
 * <p>Anti-duplication: every glass carries {@code mcbrand:glass = <owner uuid>:<serial>} in its
 * PersistentDataContainer; the owner's current serial lives in the player's own PDC (saved with the player's
 * data, nothing else stored). {@code /drink} (or the configured command) issues serial + 1, so a glass left in a chest goes stale. A sweep of
 * the owner's inventory (on join, the drink command and every sip) keeps exactly one current glass and removes stale
 * copies, other players' glasses and creative-mode copies; a stale glass that someone tries to drink is removed.
 * Brewing a glass is refused. The resource pack (optional) shows the glass model through custom_model_data
 * strings [LEGACY_MODEL_STRING]; without it the item looks like a plain potion in the configured colour.
 *
 * <p>LEGACY_* ids (v1.2): the item had one fixed look until v1.1. Its ids stay frozen: drinks that exist in
 * inventories and chests carry them (PDC key, cooldown group, custom_model_data string), and the resource pack
 * keeps its paths (item/wine_glass, font/wine.png), so a pack built from the same art stays byte-identical.
 * Never rename them.
 */
final class Drink {
    static final String LEGACY_MODEL_STRING = "mcbrand:wine_glass"; // = mcctl/brandpack.py MODEL_KEY
    static final String LEGACY_COOLDOWN_GROUP = "wine_glass"; // the use_cooldown group's key

    private final Settings s;
    private final NamespacedKey glassKey;
    private final NamespacedKey serialKey;
    private final NamespacedKey cooldownGroup;
    private final Map<UUID, Long> lastDrink = new HashMap<>();
    int drinks;
    int removed;

    Drink(BrandPlugin plugin, Settings settings) {
        this.s = settings;
        this.glassKey = new NamespacedKey(plugin, "glass");
        this.serialKey = new NamespacedKey(plugin, "glass_serial");
        this.cooldownGroup = new NamespacedKey(plugin, LEGACY_COOLDOWN_GROUP);
    }

    ItemStack create(UUID owner, int serial) {
        ItemStack item = new ItemStack(Material.POTION);
        PotionMeta meta = (PotionMeta) item.getItemMeta();
        meta.setColor(Color.fromRGB(s.drinkColor()));
        meta.customName(Texts.plain(s.drinkName(), s.accent()));
        List<Component> lore = new ArrayList<>();
        for (String line : s.drinkLore()) {
            lore.add(Texts.plain(line, s.subtle()));
        }
        meta.lore(lore);
        meta.setMaxStackSize(1);
        UseCooldownComponent cd = meta.getUseCooldown();
        cd.setCooldownSeconds(s.cooldownSeconds());
        cd.setCooldownGroup(cooldownGroup);
        meta.setUseCooldown(cd);
        CustomModelDataComponent model = meta.getCustomModelDataComponent();
        model.setStrings(List.of(LEGACY_MODEL_STRING));
        meta.setCustomModelDataComponent(model);
        meta.getPersistentDataContainer().set(glassKey, PersistentDataType.STRING, owner + ":" + serial);
        item.setItemMeta(meta);
        // no "No Effects" line: the potion contents stay out of the tooltip (tooltip_display component)
        item.setData(DataComponentTypes.TOOLTIP_DISPLAY,
                TooltipDisplay.tooltipDisplay().addHiddenComponents(DataComponentTypes.POTION_CONTENTS).build());
        return item;
    }

    /** The glass tag ("uuid:serial") of an item, or null if it isn't the drink. */
    String tag(ItemStack item) {
        if (item == null || item.getType() != Material.POTION || !item.hasItemMeta()) {
            return null;
        }
        return item.getItemMeta().getPersistentDataContainer().get(glassKey, PersistentDataType.STRING);
    }

    int serial(Player p) {
        Integer v = p.getPersistentDataContainer().get(serialKey, PersistentDataType.INTEGER);
        return v == null ? 0 : v;
    }

    /** Whether this player was never given a glass (no serial in their PDC yet): the "received" flag. */
    boolean neverIssued(Player p) {
        return !p.getPersistentDataContainer().has(serialKey, PersistentDataType.INTEGER);
    }

    String currentTag(Player p) {
        return p.getUniqueId() + ":" + serial(p);
    }

    /** Keeps one current glass in the player's inventory, removes every other glass. Returns whether one is left. */
    boolean sweep(Player p) {
        PlayerInventory inv = p.getInventory();
        String current = currentTag(p);
        boolean kept = false;
        ItemStack[] items = inv.getContents();
        for (int i = 0; i < items.length; i++) {
            String t = tag(items[i]);
            if (t == null) {
                continue;
            }
            if (!kept && t.equals(current) && items[i].getAmount() == 1) {
                kept = true;
                continue;
            }
            inv.setItem(i, null);
            removed++;
        }
        return kept;
    }

    /** The drink command and a join of someone who never had one: a new glass (serial + 1, so older copies go stale). Null if the inventory is full. */
    ItemStack issue(Player p) {
        if (p.getInventory().firstEmpty() < 0) {
            return null;
        }
        PersistentDataContainer pdc = p.getPersistentDataContainer();
        int next = serial(p) + 1;
        pdc.set(serialKey, PersistentDataType.INTEGER, next);
        sweep(p); // the previous serial's glass, if any, is stale now
        ItemStack glass = create(p.getUniqueId(), next);
        p.getInventory().addItem(glass);
        return glass;
    }

    enum Sip { OK, STALE, COOLDOWN }

    /** Decides a drink of a tagged item; on OK the caller sets the replacement (a fresh copy). */
    Sip drink(Player p, String tag, long nowMs) {
        if (!tag.equals(currentTag(p))) {
            return Sip.STALE;
        }
        Long last = lastDrink.get(p.getUniqueId());
        if (last != null && nowMs - last < s.cooldownSeconds() * 1000L - 250) {
            return Sip.COOLDOWN;
        }
        lastDrink.put(p.getUniqueId(), nowMs);
        drinks++;
        if (s.nauseaSeconds() > 0) {
            p.addPotionEffect(new PotionEffect(PotionEffectType.NAUSEA, s.nauseaSeconds() * 20, 0, true, false, true));
        }
        p.setCooldown(cooldownGroup, s.cooldownSeconds() * 20);
        return Sip.OK;
    }

    ItemStack refill(Player p) {
        return create(p.getUniqueId(), serial(p));
    }

    void forget(UUID id) {
        lastDrink.remove(id);
    }

    /** For /mcbrand selftest: build a glass and read every property back through the API. */
    String selftest() {
        UUID id = new UUID(0, 0);
        ItemStack g = create(id, 7);
        ItemMeta m = g.getItemMeta();
        UseCooldownComponent cd = m.getUseCooldown();
        String hex = String.format("#%06X", ((PotionMeta) m).getColor().asRGB());
        return "glass " + (id + ":7").equals(tag(g))
                + " model=" + m.getCustomModelDataComponent().getStrings()
                + " cooldown=" + cd.getCooldownSeconds() + "s group=" + cd.getCooldownGroup()
                + " color=" + hex + " stack=" + g.getMaxStackSize(); // no meta override: potions stack to 1
    }
}
