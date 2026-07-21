# Clean 2026 FLU table and impute various measures, using coefficients 
# estimated via the estimation_FLU2026.R script.
# Adapted from development_constraints_imputation.R (originally written by Christy Lam)
# Hana Sevcikova (PSRC)
# 05/11/2026

library(stringr)
library(purrr)
library(magrittr)
library(data.table)
library(foreign)
library(readxl)

# ---- parse command-line arguments ----
.args <- commandArgs(trailingOnly = TRUE)
# Support both --key=value and --key value formats
.arglist <- list()
.i <- 1
while (.i <= length(.args)) {
    .a <- .args[.i]
    if (grepl("^--", .a)) {
        if (grepl("=", .a)) {
            # format: --key=value
            .kv <- strsplit(sub("^--", "", .a), "=")[[1]]
            .arglist[[.kv[1]]] <- .kv[2]
            .i <- .i + 1
        } else {
            # format: --key value
            .key <- sub("^--", "", .a)
            .i <- .i + 1
            if (.i <= length(.args) && !grepl("^--", .args[.i])) {
                .arglist[[.key]] <- .args[.i]
                .i <- .i + 1
            }
        }
    } else {
        .i <- .i + 1
    }
}
in.path       <- .arglist[["input-dir"]]
out.path      <- .arglist[["output-dir"]]
master.lookup <- .arglist[["master-lookup"]]
new.flu.name  <- .arglist[["new-flu"]]
old.flu.name  <- .arglist[["old-flu"]]

# ---- require all arguments to be supplied ----
.required.args <- list("input-dir" = in.path, "output-dir" = out.path,
                        "master-lookup" = master.lookup, "new-flu" = new.flu.name,
                        "old-flu" = old.flu.name)
.missing.args <- names(.required.args)[sapply(.required.args, is.null)]
if (length(.missing.args) > 0) {
    stop("Missing required argument(s): --", paste(.missing.args, collapse = ", --"))
}

# read new FLU and do some cleaning including removing duplicates
source("load_FLU2026.R")

# read old FLU
ofluall <- fread(old.flu.name)
lu <- read_xlsx(master.lookup) %>% as.data.table
setnames(ofluall, c("Jurisdicti", "Juris_zn"), c("Juris", "juris_zn"))
# attach master_ID to old flu
lu.oflu <- lu[, .(FLU_master_id, Key = juris_zn_19)]
ofluall <- merge(ofluall, lu.oflu, all.x = TRUE, by.x = "juris_zn", by.y = "Key")
# attach master id to new flu
flu <- merge(flu, lu[, .(FLU_master_id, Key = juris_zn_26)], all.x = TRUE, 
             by.x = "juris_zn", by.y = "Key")

# set the source of the density columns to "collected"
for (i in 1:length(cols.sets)) {
  s <- cols.sets[[i]]
  use.col <-s$use
  for (density.col in s$max_dens) {
    colnm_tag <- paste0(density.col, "_src")
    equat <- parse(text = paste0("\`:=\`(", colnm_tag, " = 'collected')"))
    flu[!is.na(get(density.col)) & get(density.col) > 0, eval(equat)]
  }
  colnm_tag <- paste0(s$height, "_src")
  equat <- parse(text = paste0("\`:=\`(", colnm_tag, " = 'collected')"))
  flu[!is.na(get(s$height)) & get(s$height) > 0, eval(equat)]
}


flu[, .N, by = "rural"] # very few records have this info
process_rural(flu)

more_cleaning(flu)


paste(sort(setdiff(unique(flu[Res_Use == TRUE & (is.na(rural) | rural == TRUE), Juris]), 
        flu[!is.na(FloorMaxDU_lot)  & FloorMaxDU_lot %in% 2:6, .N, by = "Juris"][, Juris])), collapse = ", ")

# if use-specific Height not given but MaxHt_Mixed given, set the use-specific height to that
for(use in c("Res", "Comm", "Office", "Indust")){
  equat <- parse(text = paste0("\`:=\`( MaxHt_", use, " = MaxHt_Mixed, MaxHt_",  
                               use, "_src = 'asserted')"))
  flu[get(paste0(use, "_Use")) == TRUE & is.na(get(paste0("MaxHt_", use))) & !is.na(MaxHt_Mixed) & MaxHt_Mixed != 0,
      eval(equat)]
}

# if MaxHt_Mixed not given but MaxHt of other allowed uses is known, set the max of it for the mixed use
equat <- parse(text = "\`:=\`( MaxHt_Mixed = pmax(MaxHt_Res,  MaxHt_Comm, MaxHt_Office, MaxHt_Indust, na.rm = TRUE), MaxHt_Mixed_src = 'asserted')")
flu[Mixed_Use == TRUE & (is.na(MaxHt_Mixed) | MaxHt_Mixed == 0), eval(equat)]

# do the same for FAR
equat <- parse(text = "\`:=\`( MaxFAR_Mixed = pmax(MaxFAR_Res,  MaxFAR_Comm, MaxFAR_Office, MaxFAR_Indust, na.rm = TRUE), MaxFAR_Mixed_src = 'asserted')")
flu[Mixed_Use == TRUE & (is.na(MaxFAR_Mixed) | MaxFAR_Mixed == 0), eval(equat)]

# do the same for LC
equat <- parse(text = "\`:=\`( LC_Mixed = pmax(LC_Res,  LC_Comm, LC_Office, LC_Indust, na.rm = TRUE))")
flu[Mixed_Use == TRUE & (is.na(LC_Mixed) | LC_Mixed == 0), eval(equat)]

# for residential records without MaxDU_Res, MaxFAR_Res, MaxDU_lot but with MaxDU_Mixed, use that for MaxDU_Res
flu[Res_Use == TRUE & is.na(MaxDU_Res) & is.na(MaxFAR_Res) & DU_lot_valid == FALSE & !is.na(MaxDU_Mixed) & MaxDU_Mixed != 0, 
    `:=`(MaxDU_Res = MaxDU_Mixed, MaxDU_Res_src = 'asserted')]

# similarly for missing MinDU_Res - take it from MinDU_Mixed
flu[Res_Use == TRUE & !is.na(MinDU_Mixed) & DU_lot_valid == FALSE & (is.na(MinDU_Res) | MinDU_Res < MinDU_Mixed), 
    `:=`(MinDU_Res = MinDU_Mixed)]

# for mobile home parks with missing DU/acre, impute 5
flu[Zone == "MHP" & Res_Use == TRUE & is.na(MaxDU_Res) & DU_lot_valid == FALSE, 
    `:=`(MaxDU_Res = 5, MaxDU_Res_src = 'asserted')]

# adjust MaxHt_Res column
flu[, MaxHt_Res_orig := MaxHt_Res]
adj1 <- with(flu, !is.na(MaxFAR_Res) & !is.na(MaxFAR_Mixed) & MaxFAR_Res < MaxFAR_Mixed & MaxFAR_Res > 0)
adj2 <- with(flu, !is.na(MaxFAR_Res) & !is.na(MaxFAR_Mixed) & MaxFAR_Res > MaxFAR_Mixed)
flu[adj1, `:=`(MaxHt_Res = round(MaxHt_Res * MaxFAR_Res/MaxFAR_Mixed), 
                   MaxHt_Res_src = 'adjusted')]
flu[adj2, `:=`(MaxHt_Res = round(MaxHt_Res * MaxFAR_Res/(MaxFAR_Mixed + MaxFAR_Res)),
                   MaxHt_Res_src = 'adjusted')]


# for imputing FAR from height, get assumed floor height from the old flu
for (i in 1:length(cols.sets)) {
  use.col <-cols.sets[[i]]$use
  ht.col <- cols.sets[[i]]$height
  lc.col <- cols.sets[[i]]$lc
  if("far" %in% names(cols.sets[[i]]$max_dens) && cols.sets[[i]]$max_dens$far %in% colnames(ofluall)) {
    density.col <- cols.sets[[i]]$max_dens$far
  } else {
    if(is.null(names(cols.sets[[i]]$max_dens))) {
      density.col <- cols.sets[[i]]$max_dens
    } else next
  }
  if(! density.col %in% colnames(ofluall)) next
  newcolnm <- paste0("floor_height_", names(cols.sets)[i])
  density.src.col <- paste0(density.col, "_src")
  ht.src.col <- paste0(ht.col, "_src")
  equat1 <- parse(text = paste0("\`:=\`(", newcolnm, " = ", ht.col, 
                               " * ", lc.col, " / ", density.col, ")"))
  equat2 <- parse(text = paste0("\`:=\`(", newcolnm, " = ", ht.col, 
                                " * ", 0.95, " / ", density.col, ")"))
  ofluall[get(use.col) == "Y" & !is.na(get(lc.col)) & 
            !is.na(get(density.col)) & get(density.src.col) != "imputed" &
            !is.na(get(ht.col)) & get(ht.src.col) != "imputed", eval(equat1)]
  ofluall[get(use.col) == "Y" & is.na(get(lc.col)) & 
            !is.na(get(density.col)) & get(density.src.col) != "imputed" & 
            !is.na(get(ht.col)) & get(ht.src.col) != "imputed", eval(equat2)]
  # put it into the new flu
  newflu.col <- paste0("floor_height_", names(cols.sets)[i], "_prev")
  flu[ofluall, (newflu.col) := get(paste0("i.", newcolnm)), on = "FLU_master_id"]
  # clip the value within the 1st and 3rd quartile
  qrt <- summary(flu[, get(newflu.col)])[c(2, 5)]
  flu[!is.na(get(newflu.col)), (newflu.col) := pmin(pmax(qrt[1], get(newflu.col)), qrt[2])]
}

# if only height is given, impute FAR
for (i in 1:length(cols.sets)) {
  use.col <-cols.sets[[i]]$use
  ht.col <- cols.sets[[i]]$height
  lc.col <- cols.sets[[i]]$lc
  floor.ht <- cols.sets[[i]]$floor_height
  floor.ht.col <- paste0("floor_height_", names(cols.sets)[i], "_prev")
  
  if("far" %in% names(cols.sets[[i]]$max_dens)) {
    density.col <- cols.sets[[i]]$max_dens$far
  } else {
    if(is.null(names(cols.sets[[i]]$max_dens))) {
      density.col <- cols.sets[[i]]$max_dens
    } else next
  } 
  newcolnm <- density.col
  newcolnm_tag <- paste0(density.col, "_src")
  
  newcolnm.ht <- ht.col
  newcolnm_tag.ht <- paste0(ht.col, "_src")

  # for missing lot coverage values, use its median (separate for rural and not rural)
  med_lc_rural <- flu[get(use.col) == TRUE & !is.na(get(lc.col)) & get(lc.col) > 0 & rural == TRUE, median(get(lc.col))]
  med_lc_notrural <- flu[get(use.col) == TRUE & !is.na(get(lc.col)) & get(lc.col) > 0 & rural == FALSE, median(get(lc.col))]
  flu[get(use.col) == TRUE & (is.na(get(lc.col)) | get(lc.col) == 0) & rural == TRUE, (lc.col) := med_lc_rural]
  flu[get(use.col) == TRUE & (is.na(get(lc.col)) | get(lc.col) == 0) & rural == FALSE, (lc.col) := med_lc_notrural]
  
  # impute FAR via
  # FAR = height * lot_coverage / floor_height
  # if previous floor height not known
  equat1 <- parse(text = paste0("\`:=\`(", newcolnm, " = ", newcolnm.ht, 
                               " * 0.01 * ", lc.col, " / ", floor.ht, ", ", 
                               newcolnm_tag, "= 'estimated')"))
  # if we have previous floor height from old flu
  equat2 <- parse(text = paste0("\`:=\`(", newcolnm, " = ", newcolnm.ht, 
                                " * 0.01 * ", lc.col, " / ", floor.ht.col, ", ", 
                                newcolnm_tag, "= 'estimated')"))
  if(use.col == "Res_Use") {
    flu[, impute := ifelse(DU_lot_valid == FALSE & (is.na(MaxDU_Res) | MaxDU_Res == 0), TRUE, FALSE)]
  } else flu[, impute := TRUE]

  if(! floor.ht.col %in% colnames(flu)) flu[, (floor.ht.col) := NA]
  flu[get(use.col) == TRUE & impute == TRUE & is.na(get(floor.ht.col)) & 
            (is.na(get(density.col)) | get(density.col) == 0) &
            !is.na(get(newcolnm.ht)), eval(equat1)]
  flu[get(use.col) == TRUE & impute == TRUE & !is.na(get(floor.ht.col)) & 
        (is.na(get(density.col)) | get(density.col) == 0) &
        !is.na(get(newcolnm.ht)), eval(equat2)]
}
flu[, impute := NULL]

# convert FAR to DU/acre
# first compute floors to get efficiency (from ChatGPT)
idx <- with(flu, Res_Use == TRUE & !is.na(MaxFAR_Res) & MaxFAR_Res > 0 & is.na(MaxDU_Res) & DU_lot_valid == FALSE)
flu[idx & !is.na(MaxHt_Res), floors := round(MaxHt_Res/cols.sets$Res$floor_height)]
flu[idx, eff := ifelse((is.na(floors) | floors < 12) & rural == FALSE, 0.8, 0.7)]
# for non-rural MF (if it allows Mixed use), use 800sf per unit
flu[idx & Mixed_Use == TRUE & rural == FALSE, MaxDU_Res := MaxFAR_Res * 43560 * eff / 800]
# for SF (if Mixed use is not allowed) or rural MF, use 1000sf per unit
flu[idx & ((Mixed_Use == FALSE & rural == FALSE) | (Mixed_Use == TRUE & rural == TRUE)), 
    MaxDU_Res := MaxFAR_Res * 43560 * eff / 1000]
# for SF (if Mixed use is not allowed) in rural areas, use 2000sf per unit
flu[idx & Mixed_Use == FALSE & rural == TRUE, MaxDU_Res := MaxFAR_Res * 43560 * eff / 2000]
flu[idx, MaxDU_Res_src := "estimated"]
flu[, `:=`(floors = NULL, eff = NULL)]

# something simpler and more conservative for MinFAR_Res -> MinDU_Res
flu[Res_Use == TRUE & !is.na(MinFAR_Res) & MinFAR_Res > 0 & is.na(MinDU_Res) & DU_lot_valid == FALSE,
    `:=`(MinDU_Res = pmin(MinFAR_Res * 43560 * 0.8 / 2000, MaxDU_Res, na.rm = TRUE),
         MinDU_Res_src = 'estimated')]

# check which records are only Mixed use and nothing else
unique(flu[Mixed_Use == TRUE & Res_Use == FALSE & Comm_Use == FALSE & Indust_Use == FALSE & Office_Use == FALSE & grepl("residential", Definition), Juris])


# Compile previous flu ----------------------------------------------------

# clean old flu (2019)
oflu.max.cols <- str_subset(colnames(ofluall), "^Max.*")
oflu.max.cols <- setdiff(oflu.max.cols, str_subset(colnames(ofluall), "_src"))

# set values that were previously imputed to NA
for(col in oflu.max.cols){
    ofluall[get(paste0(col, "_src")) == "imputed", (col) := NA]
}

oflu.cols <- intersect(colnames(ofluall), 
                       c("FLU_master_id", "Juris", "Key", "juris_zn", "Definition", "edit", oflu.max.cols))
oflu <- ofluall[, ..oflu.cols]

# 
# Join flu to old flu -----------------------------------------------------
# 
# 
# _new = flu, _prev = old flu (2019)
# first the part that joins to the old flu
flu.join <- merge(flu[!is.na(FLU_master_id)], oflu, by = c("FLU_master_id"), 
                  suffixes = c("_new", "_prev"), all.x = TRUE #all = TRUE
                  )
# part that is new
addflu <- flu[is.na(FLU_master_id)]
# rename the column names of the new part to *_new so that it matches to the part above
renamecols <- setdiff(colnames(addflu), colnames(flu.join)) 
setnames(addflu, renamecols, paste0(renamecols, "_new"))
# put it together
flu.join <- rbind(flu.join, addflu, fill = TRUE)


# Collected & previous values -------------------------------------------

flu.imp <- flu.join[order(juris_zn_new)]

## densities ----
 
# update col ending '_imp' with prev du/far if available and copy original Max du/far to '_imp' cols
for (i in 1:length(cols.sets)) {

   print(names(cols.sets[i]))
   s <- cols.sets[[i]]
   use.col <-s$use
   
   for (density.col in s$max_dens) {
     if(!density.col %in% colnames(oflu)) next
     prev.dens.col <- paste0(density.col, "_prev")
     new.dens.col <- paste0(density.col, "_new")
     
     imp.density.col <- paste0(density.col, "_imp")
     newcolnm_tag <- paste0(density.col, "_src")
     prev.equat <- parse(text = paste0("\`:=\`(", imp.density.col, " = ", prev.dens.col, ", ", newcolnm_tag, " = 'prev')"))
     orig.equat <- parse(text = paste0("\`:=\`(", imp.density.col, " = ", new.dens.col, ")"))

     # update col ending '_imp' with original du/far
     if(density.col == "MaxDU_Res") 
       flu.imp[, impute := ifelse(DU_lot_valid == FALSE, TRUE, FALSE)]
     else flu.imp[, impute := TRUE]
     
     flu.imp[!is.na(Juris_new) &
               get(use.col) == TRUE & 
               ((!is.na(get(new.dens.col)) & get(new.dens.col) != 0) | 
               impute == FALSE), eval(orig.equat)]

    # update col ending '_imp' with prev du/far
      flu.imp[!is.na(Juris_new) & # is not null (flu.imp is a union)
              get(use.col) == TRUE & impute == TRUE &
              (is.na(get(new.dens.col)) | get(new.dens.col) == 0) &
              !is.na(get(prev.dens.col)) & get(prev.dens.col) > 0, 
            eval(prev.equat)]
   }
}
 
 cat("\n")

 flu.imp[, impute := NULL]
 
# ## Max height ----
# 
# # update original Max Ht to '_imp' cols
# # also copy prev MaxHt for 'Res' and 'Mixed' if available to col ending '_imp' 
# 
for (stype in names(cols.sets)) {
   s <- cols.sets[[stype]]
   
   use.col <-s$use
   ht.col <- s$height
   if(!ht.col %in% colnames(oflu)) next
   
   prev.ht.col <- paste0(ht.col, "_prev")
   new.ht.col <- paste0(ht.col, "_new")
   
   imp.ht.col <- paste0(ht.col, "_imp")
   newcolnm_tag <- paste0(ht.col, "_src")
   
   # update col ending '_imp' with collected height
   orig.equat <- parse(text = paste0("\`:=\`(", imp.ht.col, " = ", new.ht.col, ")"))
   
   flu.imp[!is.na(Juris_new) &
             get(use.col) == TRUE &
             !is.na(get(new.ht.col)) & get(new.ht.col) > 0, eval(orig.equat)]
   
  # update col ending '_imp' with prev height
  prev.equat <- parse(text = paste0("\`:=\`(", imp.ht.col, "= ", prev.ht.col, ",", newcolnm_tag, "= 'prev')"))

  flu.imp[!is.na(Juris_new) &
            get(eval(use.col)) == TRUE &
            is.na(get(imp.ht.col)) &
            is.na(get(new.ht.col)) &
            !is.na(get(prev.ht.col)) & get(prev.ht.col) > 0, eval(prev.equat)]

 }
 
# Impute values -----------------------------------------------------------

#flu.imp <- flu.imp[!is.na(Juris_new)]
 

# coefficients (estimated via estimation_FLU2026.R)
#coeff <- list(a = 1.403, b = 0.654, c = 2.121, q = -0.980, d = -2.880, e = 1.448, r = -2.187)
coeff <- list(a = -1.382108, b = 0.852237, c = 0.017928, q = -0.839707, 
              d = -1.9392, e = 1.2767, r = -1.2108)


no.info.rows <- flu.imp[Res_Use == TRUE & is.na(LC_Res) & is.na(MaxHt_Res_imp) & 
                          is.na(MaxFAR_Res) & is.na(MaxDU_Res_imp) & DU_lot_valid == FALSE]

# Impute max DU/ac, height, and FAR
# Update 'Max_XXX_imp' columns with imputed values if criteria is met and tag 'Max_XXX_src' column as 'imputed'
for (i in 1:length(cols.sets)) {
  print(names(cols.sets[i]))
  
  use.col <-cols.sets[[i]]$use
  ht.col <- cols.sets[[i]]$height
  lc.col <- cols.sets[[i]]$lc
  
  for (j in cols.sets[[i]]$max_dens) {
    if(j %in% c("MaxFAR_Res", "FloorMaxDU_lot")) next
    print(j)

    newcolnm <- paste0(j, "_imp")
    newcolnm_tag <- paste0(j, "_src")
    
    newcolnm.ht <- paste0(ht.col, "_imp")
    newcolnm_tag.ht <- paste0(ht.col, "_src")
    
    density.col <- paste0(j, "_imp")

    if (str_detect(j, "DU") && !str_detect(j, "lot")) {
      
      # Records with missing DU/acre, non-missing heights and non-missing lot coverage(LC)
      # DU/acre = exp(a + b*log(height) + c*log(LC) + q*I(rural))
      equat1 <- parse(text = paste0("\`:=\`(", newcolnm, "= (exp(",coeff$a," + ",
                                    coeff$b,"*log(", newcolnm.ht, ") + ",
                                    coeff$c,"*log(", lc.col, ") + ",
                                    coeff$q, "*I(rural))),",
                                    newcolnm_tag, "= 'imputed')"))
      
      flu.imp[get(use.col) == TRUE & DU_lot_valid == FALSE &
            (is.na(get(density.col)) | get(density.col) == 0) &
            !is.na(get(newcolnm.ht)) & get(newcolnm.ht) > 0 &
            !is.na(get(lc.col)) & get(lc.col) > 0 & 
              is.na(get(newcolnm)), eval(equat1)]

      # Records with missing DU/acre, non-missing heights and missing lot coverage
      # DU/acre = exp(d + e*log(height)+ r*I(rural))
      equat2 <- parse(text = paste0("\`:=\`(", newcolnm, "= (exp(", coeff$d ,"+",
                                    coeff$e,"*log(", newcolnm.ht, ") + ",
                                    coeff$r, "*I(rural))),",
                                    newcolnm_tag, "= 'imputed')"))

      flu.imp[get(use.col) == TRUE  & DU_lot_valid == FALSE &
            (is.na(get(density.col)) | get(density.col) == 0) &
            !is.na(get(newcolnm.ht)) & 
              is.na(get(lc.col)) & get(lc.col) > 0 &
              is.na(get(newcolnm)), eval(equat2)]

      # Records with missing height, non-missing DU/acre and non-missing lot coverage:
      # height = exp[(log(DU/acre) - a - c*log(LC) - q*I(rural))/b]
      equat3 <- parse(text = paste0("\`:=\`(", newcolnm.ht, "= (exp((log(", density.col,") - ",
                                    coeff$a,"-",
                                    coeff$c,"*log(", lc.col,") -",
                                    coeff$r, "*I(rural))/",
                                    coeff$b ,")),",
                                    newcolnm_tag.ht, "= 'imputed')"))
      flu.imp[get(use.col) == TRUE & 
            (is.na(get(newcolnm.ht)) | get(newcolnm.ht) == 0) &
            (!is.na(get(density.col)) | get(density.col) != 0) &
            (!is.na(get(lc.col)) | get(lc.col) != 0), eval(equat3)]

      # Records with missing height, non-missing DU/acre and missing lot coverage:
      # height = exp[(log(DU/acre) - d - r*I(rural))/e]
      equat4 <- parse(text = paste0("\`:=\`(", newcolnm.ht, "= (exp((log(", density.col,")-",
                                    coeff$d," -",
                                    coeff$r, "*I(rural))/",
                                    coeff$e,")),",
                                    newcolnm_tag.ht, "= 'imputed')"))
      flu.imp[get(eval(use.col)) == TRUE &
            (is.na(get(newcolnm.ht)) | get(newcolnm.ht) == 0) &
            (!is.na(get(density.col)) | get(density.col) != 0) &
            (is.na(get(lc.col)) | get(lc.col) == 0), eval(equat4)]

    } else { # non-residential imputation
      # TODO: Revise. This block might not be needed as 
      # the LC and FAR imputation was put to the front of this procedure. 
      #
      # for missing lot coverage values, use its median 
      med_lc <- flu.imp[get(use.col) == TRUE & !is.na(get(lc.col)) & get(lc.col) > 0, median(get(lc.col))]
      flu.imp[get(use.col) == TRUE & is.na(get(lc.col)) | get(lc.col) == 0, (lc.col) := med_lc]

      # impute FAR for non-res use via
      # FAR = height * lot_coverage / 12
      equat <- parse(text = paste0("\`:=\`(", newcolnm, " = ", newcolnm.ht, 
                                   " * 0.01 * ", lc.col, " / 12, ", newcolnm_tag, "= 'imputed')"))
      flu.imp[get(use.col) == TRUE &
            (is.na(get(density.col)) | get(density.col) == 0) &
            !is.na(get(newcolnm.ht)) &
              is.na(get(newcolnm)), eval(equat)]
      
      # impute height for non-res use
      # height = pmax(12, 12*FAR/lot_coverage)
      equat.nonres.ht <- parse(text = paste0("\`:=\`(", newcolnm.ht, "= pmax(12, 12 * ", newcolnm,"/(", lc.col,"/100)), ", newcolnm_tag.ht, "= 'imputed')"))
      flu.imp[get(use.col) == TRUE & !is.na(get(newcolnm)) &
                (is.na(get(newcolnm.ht)) | get(newcolnm.ht) == 0), eval(equat.nonres.ht)]
      miss <- flu.imp[get(use.col) == TRUE & is.na(get(newcolnm))]
      cat("Missing info for ", nrow(miss),
          " records: ", paste(unique(miss[, juris_zn_new]), collapse = ", "), "\n")
    }
  }
}

# remove invalid values of MaxDU_lot
#flu.imp[!is.na(MaxDU_lot) & DU_lot_valid == FALSE, `:=`(MaxDU_lot = NA, MaxDU_lot_src = 'deleted')]
flu.imp[, DU_lot_valid := NULL]

# exclude _prev records that didn't match current flu records
flu.fin.prep <- flu.imp[!is.na(juris_zn_new)]

# convert logical types to integer
for(col in use.cols){
  flu.fin.prep[, (col) := as.integer(get(col))]
}
flu.fin.prep[, rural := as.integer(rural)]

## temp output for QC (kitchen sink file) ----
fwrite(flu.fin.prep, file.path(out.path, paste0("temp_flu_imputed_", Sys.Date(), ".csv")))

# Final output ------------------------------------------------------------


# subset columns and rename in preparation for 'unroll_constraints' .py script
ff.types <- c("Res", "Mixed", "Office", "Indust", "Comm")
ff.max.cols <- c(paste0("MaxDU_", ff.types), paste0("MaxFAR_", ff.types), paste0("MaxHt_", ff.types))
ff.excl.cols <- c(str_subset(colnames(flu.fin.prep), "_prev"), ff.max.cols,
                  "MaxHt_Res_orig", "edit", "Key", "ADU notes", "Public_Use", "PUD_Use", 
                  str_subset(colnames(flu.fin.prep), "Unnamed"))

ff.cols <- setdiff(colnames(flu.fin.prep), ff.excl.cols)
flu.fin.prep <- flu.fin.prep[, ..ff.cols] 

# remove '_imp' from col name
colnames(flu.fin.prep) <- str_trim(str_replace_all(colnames(flu.fin.prep), "_imp", ""))
ff.excl.new.cols <- intersect(colnames(flu.fin.prep), 
                              str_trim(str_replace_all(str_subset(colnames(flu.fin.prep), "_new"), "_new", "")))
ff.cols <- setdiff(colnames(flu.fin.prep), paste0(ff.excl.new.cols, "_new"))
flu.fin.prep <- flu.fin.prep[, ..ff.cols]
colnames(flu.fin.prep) <- str_trim(str_replace_all(colnames(flu.fin.prep), "_new", ""))

# remove duplicate rows (in preparation for assigning plan_type_ids in unroll_constraints.py)
gb.cols <- setdiff(colnames(flu.fin.prep), c("Bonus_avail", "MinDU_Comm", "MinDU_Office",
                                             "MinDU_Indust", "MinDU_Mixed", "MinFAR_Res", "MaxDU_Mixed"))
# order the columns the way they should be in the output file
ordered.cols <- c("FLU_master_id", id.cols, "Bonus_included", use.cols, "FloorMaxDU_lot", 
                  min.cols, max.cols, maxht.cols, lc.cols, "rural")
gb.cols <- intersect(c(ordered.cols, setdiff(gb.cols, ordered.cols)), gb.cols)

flu.fin <- unique(flu.fin.prep[, ..gb.cols], by = gb.cols, fromLast = T)

## output for use with unroll_constraints.py ----
fwrite(flu.fin, file.path(out.path, paste0("final_flu_imputed_", Sys.Date(), ".csv")))

#flu.fin[MaxDU_Res_src == "prev"]

# summary of changes
dt <- flu.fin[, str_subset(colnames(flu.fin), "_src"), with = FALSE]

# counts
dcast(
  melt(dt,
       measure.vars = names(dt),
       variable.name = "column",
       value.name = "value"
  )[
    , .N, by = .(value, column)
  ],
  value ~ column,
  value.var = "N",
  fill = 0
)
# percentages
dcast(
  melt(dt,
       measure.vars = names(dt),
       variable.name = "column",
       value.name = "value"
  )[
    , .(pct = round(.N / nrow(dt) * 100, 1)), by = .(value, column)
  ],
  value ~ column,
  value.var = "pct",
  fill = 0
)





